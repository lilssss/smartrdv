"""
Database — SmartRDV
====================
SQLite + SQLAlchemy
Tables : practitioners, slots, bookings, users
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    Boolean, DateTime, Text, ForeignKey, JSON
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session

DATABASE_URL = "sqlite:///./smartrdv.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # nécessaire pour SQLite + FastAPI
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ── Modèles ───────────────────────────────────────────────────

class Practitioner(Base):
    __tablename__ = "practitioners"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String, index=True)
    specialty   = Column(String, index=True)
    address     = Column(String, default="")
    city        = Column(String, default="")
    profile_url = Column(String, default="")
    agenda_ids        = Column(JSON, default=[])
    practice_ids      = Column(JSON, default=[])
    visit_motive_ids  = Column(JSON, default=[])
    scraped_at  = Column(DateTime, default=datetime.utcnow)
    slots_count = Column(Integer, default=0)

    slots    = relationship("Slot",    back_populates="practitioner", cascade="all, delete-orphan")
    bookings = relationship("Booking", back_populates="practitioner")


class Slot(Base):
    __tablename__ = "slots"

    id               = Column(Integer, primary_key=True, index=True)
    practitioner_id  = Column(Integer, ForeignKey("practitioners.id"), index=True)
    start_date       = Column(String, index=True)
    end_date         = Column(String, default="")
    agenda_id        = Column(Integer, default=0)
    visit_motive_id  = Column(Integer, default=0)
    source           = Column(String, default="api")   # "api" ou "dom"
    scraped_at       = Column(DateTime, default=datetime.utcnow)

    practitioner = relationship("Practitioner", back_populates="slots")


class Booking(Base):
    __tablename__ = "bookings"

    id              = Column(Integer, primary_key=True, index=True)
    practitioner_id = Column(Integer, ForeignKey("practitioners.id"), nullable=True)
    profile_url     = Column(String, default="")
    slot_datetime   = Column(String)
    user_email      = Column(String, default="")
    is_new_patient  = Column(Boolean, default=True)
    motive_keyword  = Column(String, default="")
    is_teleconsult  = Column(Boolean, default=False)
    status          = Column(String, default="pending")  # pending, success, error
    message         = Column(Text, default="")
    created_at      = Column(DateTime, default=datetime.utcnow)

    practitioner = relationship("Practitioner", back_populates="bookings")


class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    email           = Column(String, unique=True, index=True)
    session_cookies = Column(Text, default="[]")   # JSON stringifié
    logged_at       = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)


# ── Init ──────────────────────────────────────────────────────

def init_db():
    """Crée toutes les tables si elles n'existent pas."""
    Base.metadata.create_all(bind=engine)
    print("[DB] Tables initialisées ✅")


def get_db():
    """Dépendance FastAPI pour obtenir une session DB."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Helpers ───────────────────────────────────────────────────

def save_crawl_to_db(db: Session, practitioners_data: list, slots_data: list, specialty: str, city: str):
    """
    Sauvegarde les résultats d'un crawl en base.
    Supprime les anciens praticiens de la même spécialité/ville avant d'insérer.
    """
    # Supprime les anciens pour cette spécialité+ville
    old = db.query(Practitioner).filter(
        Practitioner.specialty == specialty,
        Practitioner.city == city,
    ).all()
    for p in old:
        db.delete(p)
    db.commit()

    # Insère les nouveaux praticiens
    prac_map = {}  # id_local → objet DB
    for p in practitioners_data:
        prac = Practitioner(
            name             = p.get("name", "Inconnu"),
            specialty        = specialty,
            address          = p.get("address", ""),
            city             = city,
            profile_url      = p.get("profile_url", ""),
            agenda_ids       = p.get("agenda_ids", []),
            practice_ids     = p.get("practice_ids", []),
            visit_motive_ids = p.get("visit_motive_ids", []),
            slots_count      = p.get("slots_count", 0),
            scraped_at       = datetime.utcnow(),
        )
        db.add(prac)
        db.flush()  # obtient l'id généré
        prac_map[p.get("id")] = prac

    # Insère les créneaux
    for s in slots_data:
        pid = s.get("practitioner_id")
        prac = prac_map.get(pid)
        if not prac:
            continue
        slot = Slot(
            practitioner_id = prac.id,
            start_date      = s.get("start_date", ""),
            end_date        = s.get("end_date", ""),
            agenda_id       = s.get("agenda_id", 0),
            visit_motive_id = s.get("visit_motive_id", 0),
            source          = s.get("source", "api"),
            scraped_at      = datetime.utcnow(),
        )
        db.add(slot)

    db.commit()
    print(f"[DB] Sauvegardé : {len(practitioners_data)} praticiens, {len(slots_data)} créneaux")


def load_from_db(db: Session, specialty: str, city: str):
    """
    Charge les praticiens et créneaux depuis la DB.
    Retourne (practitioners_data, slots_data) au format compatible avec scraped_loader.
    """
    pracs = db.query(Practitioner).filter(
        Practitioner.specialty == specialty,
        Practitioner.city.ilike(f"%{city}%"),
    ).all()

    if not pracs:
        return [], []

    practitioners_data = []
    slots_data = []

    for p in pracs:
        practitioners_data.append({
            "id":               p.id,
            "name":             p.name,
            "specialty":        p.specialty,
            "address":          p.address,
            "city":             p.city,
            "profile_url":      p.profile_url,
            "agenda_ids":       p.agenda_ids or [],
            "practice_ids":     p.practice_ids or [],
            "visit_motive_ids": p.visit_motive_ids or [],
            "slots_count":      p.slots_count,
        })
        for s in p.slots:
            slots_data.append({
                "start_date":      s.start_date,
                "end_date":        s.end_date,
                "agenda_id":       s.agenda_id,
                "visit_motive_id": s.visit_motive_id,
                "practitioner_id": p.id,
                "source":          s.source,
            })

    print(f"[DB] Chargé : {len(practitioners_data)} praticiens, {len(slots_data)} créneaux")
    return practitioners_data, slots_data


def save_booking(db: Session, **kwargs) -> Booking:
    """Sauvegarde un booking en base."""
    booking = Booking(**kwargs)
    db.add(booking)
    db.commit()
    db.refresh(booking)
    return booking


def get_booking_history(db: Session, user_email: str = None) -> list:
    """Retourne l'historique des bookings."""
    q = db.query(Booking)
    if user_email:
        q = q.filter(Booking.user_email == user_email)
    return q.order_by(Booking.created_at.desc()).limit(50).all()


# ── Entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("Base de données SmartRDV créée → smartrdv.db")
