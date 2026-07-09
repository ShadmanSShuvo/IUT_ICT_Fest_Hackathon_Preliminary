from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal, Base, engine
from app.models import Organization, User, Room, Booking
from app.services import ratelimit, reference, stats

client = TestClient(app)

@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Clear services states if any
    ratelimit._buckets.clear()
    stats._stats.clear()
    reference._counter["value"] = 1000

def _future(hours: int) -> str:
    return (datetime.utcnow() + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()

def test_registration_and_login():
    # Test register unique
    r1 = client.post("/auth/register", json={"org_name": "OrgA", "username": "alice", "password": "pw"})
    assert r1.status_code == 201
    
    # Test duplicate username in same org
    r2 = client.post("/auth/register", json={"org_name": "OrgA", "username": "alice", "password": "pw2"})
    assert r2.status_code == 409
    assert r2.json()["code"] == "USERNAME_TAKEN"
    
    # Test register same username in different org
    r3 = client.post("/auth/register", json={"org_name": "OrgB", "username": "alice", "password": "pw"})
    assert r3.status_code == 201
    
    # Test login success
    l1 = client.post("/auth/login", json={"org_name": "OrgA", "username": "alice", "password": "pw"})
    assert l1.status_code == 200
    assert "access_token" in l1.json()

def test_refresh_token_rotation():
    client.post("/auth/register", json={"org_name": "OrgA", "username": "alice", "password": "pw"})
    l1 = client.post("/auth/login", json={"org_name": "OrgA", "username": "alice", "password": "pw"})
    ref_token = l1.json()["refresh_token"]
    
    # Use refresh token once
    rf1 = client.post("/auth/refresh", json={"refresh_token": ref_token})
    assert rf1.status_code == 200
    
    # Reuse refresh token (should fail)
    rf2 = client.post("/auth/refresh", json={"refresh_token": ref_token})
    assert rf2.status_code == 401
    assert "already used" in rf2.json()["detail"].lower()

def test_booking_windows_and_durations():
    # Setup user/room
    reg = client.post("/auth/register", json={"org_name": "OrgA", "username": "alice", "password": "pw"})
    token = client.post("/auth/login", json={"org_name": "OrgA", "username": "alice", "password": "pw"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    room = client.post("/rooms", json={"name": "Room1", "capacity": 2, "hourly_rate_cents": 100}, headers=headers).json()
    room_id = room["id"]
    
    # Test past start time
    past_start = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
    end = _future(2)
    b1 = client.post("/bookings", json={"room_id": room_id, "start_time": past_start, "end_time": end}, headers=headers)
    assert b1.status_code == 400
    
    # Test end_time <= start_time
    start = _future(5)
    b2 = client.post("/bookings", json={"room_id": room_id, "start_time": start, "end_time": start}, headers=headers)
    assert b2.status_code == 400
    assert b2.json()["code"] == "INVALID_BOOKING_WINDOW"
    
    # Test duration out of range
    b3 = client.post("/bookings", json={"room_id": room_id, "start_time": start, "end_time": _future(14)}, headers=headers)
    assert b3.status_code == 400
    assert b3.json()["code"] == "INVALID_BOOKING_WINDOW"

def test_back_to_back_bookings():
    reg = client.post("/auth/register", json={"org_name": "OrgA", "username": "alice", "password": "pw"})
    token = client.post("/auth/login", json={"org_name": "OrgA", "username": "alice", "password": "pw"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    room = client.post("/rooms", json={"name": "Room1", "capacity": 2, "hourly_rate_cents": 100}, headers=headers).json()
    room_id = room["id"]
    
    t1 = _future(5)
    t2 = _future(7)
    t3 = _future(9)
    
    # Book 5 to 7
    b1 = client.post("/bookings", json={"room_id": room_id, "start_time": t1, "end_time": t2}, headers=headers)
    assert b1.status_code == 201
    
    # Book 7 to 9 (back-to-back, should be allowed)
    b2 = client.post("/bookings", json={"room_id": room_id, "start_time": t2, "end_time": t3}, headers=headers)
    assert b2.status_code == 201

def test_multi_tenancy_export_leak():
    # Org A Admin
    client.post("/auth/register", json={"org_name": "OrgA", "username": "adminA", "password": "pw"})
    tokenA = client.post("/auth/login", json={"org_name": "OrgA", "username": "adminA", "password": "pw"}).json()["access_token"]
    headersA = {"Authorization": f"Bearer {tokenA}"}
    
    # Org B Admin
    client.post("/auth/register", json={"org_name": "OrgB", "username": "adminB", "password": "pw"})
    tokenB = client.post("/auth/login", json={"org_name": "OrgB", "username": "adminB", "password": "pw"}).json()["access_token"]
    headersB = {"Authorization": f"Bearer {tokenB}"}
    
    # Create room in Org B
    roomB = client.post("/rooms", json={"name": "RoomB", "capacity": 5, "hourly_rate_cents": 500}, headers=headersB).json()
    room_b_id = roomB["id"]
    
    # Admin A attempts to export room B
    exp = client.get(f"/admin/export?room_id={room_b_id}&include_all=true", headers=headersA)
    assert exp.status_code == 404
    assert exp.json()["code"] == "ROOM_NOT_FOUND"

def test_exact_48_hour_refund():
    # Admin
    client.post("/auth/register", json={"org_name": "OrgA", "username": "adminA", "password": "pw"})
    token = client.post("/auth/login", json={"org_name": "OrgA", "username": "adminA", "password": "pw"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    room = client.post("/rooms", json={"name": "Room1", "capacity": 2, "hourly_rate_cents": 100}, headers=headers).json()
    room_id = room["id"]
    
    # Make booking 48 hours + 5 seconds in the future
    now = datetime.utcnow()
    start_time = now + timedelta(hours=48, seconds=5)
    end_time = start_time + timedelta(hours=2)
    
    # We parse and format ISO string to simulate API request precisely
    booking = client.post("/bookings", json={
        "room_id": room_id,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat()
    }, headers=headers).json()
    
    # Cancel booking immediately (notice should be >= 48 hours, so 100% refund)
    cancel = client.post(f"/bookings/{booking['id']}/cancel", headers=headers)
    assert cancel.status_code == 200
    assert cancel.json()["refund_percent"] == 100
