"""Local API for private consultant, offer, and policy configuration."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.commercial_configuration import (
    CommercialOffer, CommercialPolicy, CommercialProfile, list_offers,
    read_policy, read_profile, save_offer, save_policy, save_profile,
    select_offer,
)


router = APIRouter(prefix="/api/v1/commercial-configuration", tags=["commercial configuration"])


@router.get("/profile", response_model=Optional[CommercialProfile])
def get_profile(session: Session = Depends(get_db)):
    return read_profile(session)


@router.put("/profile", response_model=CommercialProfile)
def put_profile(profile: CommercialProfile, session: Session = Depends(get_db)):
    return save_profile(session, profile)


@router.get("/offers", response_model=list[CommercialOffer])
def get_offers(session: Session = Depends(get_db)):
    return list_offers(session)


@router.get("/offers/default", response_model=Optional[CommercialOffer])
def get_default_offer(session: Session = Depends(get_db)):
    return select_offer(session)


@router.get("/offers/{code}", response_model=CommercialOffer)
def get_offer(code: str, session: Session = Depends(get_db)):
    offer = select_offer(session, code)
    if offer is None:
        raise HTTPException(status_code=404, detail="offer not configured")
    return offer


@router.put("/offers/{code}", response_model=CommercialOffer)
def put_offer(code: str, offer: CommercialOffer, session: Session = Depends(get_db)):
    if offer.code != code:
        raise HTTPException(status_code=422, detail="offer code mismatch")
    try:
        return save_offer(session, offer)
    except ValueError as error:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/policy", response_model=Optional[CommercialPolicy])
def get_policy(session: Session = Depends(get_db)):
    return read_policy(session)


@router.put("/policy", response_model=CommercialPolicy)
def put_policy(policy: CommercialPolicy, session: Session = Depends(get_db)):
    return save_policy(session, policy)
