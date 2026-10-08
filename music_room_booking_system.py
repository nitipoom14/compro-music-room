#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ระบบจองห้องดนตรี (Music Room BOOKING System)
==================================================
โครงงาน Python File I/O — ใช้ไฟล์ไบนารี + struct (CRUD: Add/Update/Delete/View)
เวอร์ชันนี้เป็นระบบ "จองห้อง" เต็มรูปแบบ: มีชื่อลูกค้า วันที่ เวลาเริ่ม-เลิก
ตรวจสอบเวลาซ้อนทับ (conflict) และคำนวณราคาอัตโนมัติ โดยบันทึกเป็นระเบียนการจอง
แยกต่างหากจากระเบียนห้อง (ไม่ใช่แค่ flag Reserved yes/no เหมือนเวอร์ชันก่อนหน้า)

ไฟล์ที่ระบบใช้ (ทั้งหมดเป็น binary, Little-Endian, fixed-length record):
  1) rooms.dat      -> ระเบียนห้องดนตรี (master data)
  2) bookings.dat   -> ระเบียนการจอง (transaction record) — หัวใจของระบบ
  3) bookings.idx   -> ดัชนีสรุปการจองต่อห้อง (room_id -> จำนวนจอง Active, จองล่าสุด)
  4) report.txt     -> รายงานสรุป (text report)

ใช้เฉพาะ Python Standard Library: struct, os, time, datetime, sys
Python 3.10+
"""

import os
import sys
import struct
import time
from datetime import datetime, date, timezone, timedelta

# ============================================================
# 1) STRUCT DEFINITIONS  (สเปกไฟล์และระเบียน)
# ============================================================

# --- rooms.dat : ระเบียนห้องดนตรี (master data, fixed-length) ---
#   room_id     i   4   รหัสห้องดนตรี (unique, primary key)
#   room_name   20s 20  ชื่อห้อง (UTF-8, fixed 20 bytes, pad \x00)
#   room_type   15s 15  ประเภทห้อง (UTF-8, fixed 15 bytes, pad \x00)
#   capacity    i   4   ความจุ (จำนวนคน)
#   floor       i   4   ชั้นที่ตั้ง
#   rate_thb    f   4   อัตราค่าบริการ (บาท/ชม.)
#   status      i   4   1 = Active, 0 = Deleted
ROOM_FMT = "<i20s15siifi"
ROOM_STRUCT = struct.Struct(ROOM_FMT)
ROOM_SIZE = ROOM_STRUCT.size  # 55 bytes
NAME_LEN = 20
TYPE_LEN = 15

# --- bookings.dat : ระเบียนการจอง (fixed-length) — หัวใจของระบบจอง ---
#   booking_id      i   4   รหัสการจอง (unique, primary key)
#   room_id         i   4   อ้างอิงไปยัง rooms.dat.room_id
#   customer_name   60s 60  ชื่อลูกค้า (UTF-8, fixed 60 bytes, pad \x00 — กันชื่อไทยขาด)
#   booking_date    i   4   วันที่จอง เก็บเป็น YYYYMMDD (เช่น 20260925)
#   start_time      i   4   เวลาเริ่ม เก็บเป็น HHMM (เช่น 1400 = 14:00)
#   end_time        i   4   เวลาเลิก เก็บเป็น HHMM (เช่น 1600 = 16:00)
#   hours           f   4   จำนวนชั่วโมงที่จอง (คำนวณอัตโนมัติ)
#   total_price     f   4   ราคารวม = hours * rate_thb ของห้อง ณ เวลาที่จอง
#   status          i   4   1 = Booked(กำลังจอง), 0 = Cancelled(ยกเลิกแล้ว)
#   created_ts      i   4   เวลาที่สร้างระเบียนนี้ (Unix timestamp)
#   หมายเหตุ: ชื่อลูกค้าอาจเป็นภาษาไทย ซึ่ง UTF-8 ใช้ ~3 ไบต์/ตัวอักษร (บวกสระ/วรรณยุกต์
#   ที่เป็นอักขระเติมแยกอีก) จึงตั้งขนาดฟิลด์นี้ไว้ 60 ไบต์ (~15-20 ตัวอักษรไทย) เพื่อกันชื่อขาด
BOOKING_FMT = "<ii60siiiffii"
BOOKING_STRUCT = struct.Struct(BOOKING_FMT)
BOOKING_SIZE = BOOKING_STRUCT.size  # 96 bytes
CUSTOMER_LEN = 60

# --- bookings.idx : ดัชนีสรุปการจองต่อห้อง (fixed-length) ---
#   room_id             i   4   อ้างอิงไปยัง rooms.dat.room_id (หนึ่งระเบียนต่อหนึ่งห้อง)
#   active_count        i   4   จำนวนการจองที่ยัง Active (status=1) ของห้องนี้
#   last_booking_id     i   4   booking_id ล่าสุดที่ถูกสร้าง/แก้ไขของห้องนี้
IDX_FMT = "<iii"
IDX_STRUCT = struct.Struct(IDX_FMT)
IDX_SIZE = IDX_STRUCT.size  # 12 bytes

DAT_FILE = "rooms.dat"
BOOKING_FILE = "bookings.dat"
IDX_FILE = "bookings.idx"
REPORT_FILE = "report.txt"

APP_VERSION = "2.0 (Booking)"
TZ_BKK = timezone(timedelta(hours=7))

STATUS_BOOKED, STATUS_CANCELLED = 1, 0


# ============================================================
# 2) PACK / UNPACK HELPERS
# ============================================================

def pack_str(s: str, size: int) -> bytes:
    b = s.encode("utf-8")[:size]
    return b.ljust(size, b"\x00")


def unpack_str(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")


class Room:
    __slots__ = ("room_id", "name", "type_", "capacity", "floor", "rate", "status")

    def __init__(self, room_id, name, type_, capacity, floor, rate, status):
        self.room_id = room_id
        self.name = name
        self.type_ = type_
        self.capacity = capacity
        self.floor = floor
        self.rate = rate
        self.status = status  # 1 active, 0 deleted

    def pack(self) -> bytes:
        return ROOM_STRUCT.pack(
            self.room_id, pack_str(self.name, NAME_LEN), pack_str(self.type_, TYPE_LEN),
            self.capacity, self.floor, self.rate, self.status,
        )

    @staticmethod
    def unpack(raw: bytes) -> "Room":
        room_id, name_b, type_b, capacity, floor, rate, status = ROOM_STRUCT.unpack(raw)
        return Room(room_id, unpack_str(name_b), unpack_str(type_b), capacity, floor, rate, status)

    def status_text(self):
        return "Active" if self.status == 1 else "Deleted"


class Booking:
    __slots__ = ("booking_id", "room_id", "customer_name", "booking_date", "start_time",
                 "end_time", "hours", "total_price", "status", "created_ts")

    def __init__(self, booking_id, room_id, customer_name, booking_date, start_time,
                 end_time, hours, total_price, status, created_ts):
        self.booking_id = booking_id
        self.room_id = room_id
        self.customer_name = customer_name
        self.booking_date = booking_date      # int YYYYMMDD
        self.start_time = start_time          # int HHMM
        self.end_time = end_time              # int HHMM
        self.hours = hours
        self.total_price = total_price
        self.status = status                  # 1 booked, 0 cancelled
        self.created_ts = created_ts

    def pack(self) -> bytes:
        return BOOKING_STRUCT.pack(
            self.booking_id, self.room_id, pack_str(self.customer_name, CUSTOMER_LEN),
            self.booking_date, self.start_time, self.end_time,
            self.hours, self.total_price, self.status, self.created_ts,
        )

    @staticmethod
    def unpack(raw: bytes) -> "Booking":
        (booking_id, room_id, cust_b, bdate, stime, etime, hours, price, status, created_ts) = \
            BOOKING_STRUCT.unpack(raw)
        return Booking(booking_id, room_id, unpack_str(cust_b), bdate, stime, etime,
                        hours, price, status, created_ts)

    def status_text(self):
        return "Booked" if self.status == 1 else "Cancelled"

    def date_str(self):
        return fmt_date(self.booking_date)

    def time_range_str(self):
        return f"{fmt_time(self.start_time)}-{fmt_time(self.end_time)}"


# ============================================================
# 3) DATE / TIME HELPERS
# ============================================================

def parse_date(s: str):
    """รับ 'YYYY-MM-DD' หรือ 'DD/MM/YYYY' -> int YYYYMMDD หรือ None ถ้าไม่ถูกต้อง"""
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            return int(d.strftime("%Y%m%d"))
        except ValueError:
            continue
    return None


def fmt_date(v: int) -> str:
    s = str(v)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def parse_time(s: str):
    """รับ 'HH:MM' -> int HHMM หรือ None ถ้าไม่ถูกต้อง"""
    s = s.strip()
    try:
        t = datetime.strptime(s, "%H:%M").time()
        return t.hour * 100 + t.minute
    except ValueError:
        return None


def fmt_time(v: int) -> str:
    hh = v // 100
    mm = v % 100
    return f"{hh:02d}:{mm:02d}"


def time_to_minutes(hhmm: int) -> int:
    return (hhmm // 100) * 60 + (hhmm % 100)


# ============================================================
# 4) DATABASE LAYER
# ============================================================

class MusicRoomBookingDB:
    def __init__(self, dat_path=DAT_FILE, booking_path=BOOKING_FILE, idx_path=IDX_FILE):
        self.dat_path = dat_path
        self.booking_path = booking_path
        self.idx_path = idx_path
        for p in (self.dat_path, self.booking_path, self.idx_path):
            if not os.path.exists(p):
                open(p, "wb").close()

    # ---------------- rooms.dat ----------------
    def dat_count(self) -> int:
        return os.path.getsize(self.dat_path) // ROOM_SIZE

    def read_room_at(self, index: int):
        with open(self.dat_path, "rb") as f:
            f.seek(index * ROOM_SIZE)
            raw = f.read(ROOM_SIZE)
        if len(raw) != ROOM_SIZE:
            return None
        try:
            return Room.unpack(raw)
        except struct.error:
            return None

    def all_rooms(self):
        out = []
        for i in range(self.dat_count()):
            r = self.read_room_at(i)
            if r is not None:
                out.append((i, r))
        return out

    def find_room_by_id(self, room_id: int):
        for i, r in self.all_rooms():
            if r.room_id == room_id and r.status == 1:
                return i, r
        return None, None

    def find_free_room_slot(self):
        for i, r in self.all_rooms():
            if r.status == 0:
                return i
        return None

    def write_room_at(self, index: int, room: Room):
        with open(self.dat_path, "r+b") as f:
            f.seek(index * ROOM_SIZE)
            f.write(room.pack())
            f.flush()
            os.fsync(f.fileno())

    def append_room(self, room: Room):
        with open(self.dat_path, "ab") as f:
            f.write(room.pack())
            f.flush()
            os.fsync(f.fileno())

    def add_room(self, room_id, name, type_, capacity, floor, rate):
        _, existing = self.find_room_by_id(room_id)
        if existing is not None:
            raise ValueError(f"มีห้องรหัส {room_id} ที่ Active อยู่แล้ว")
        room = Room(room_id, name, type_, capacity, floor, rate, status=1)
        slot = self.find_free_room_slot()
        if slot is not None:
            self.write_room_at(slot, room)
        else:
            self.append_room(room)
        return room

    def update_room(self, room_id, **fields):
        idx, room = self.find_room_by_id(room_id)
        if room is None:
            raise ValueError(f"ไม่พบห้องรหัส {room_id}")
        for key in ("name", "type_", "capacity", "floor", "rate"):
            if key in fields and fields[key] is not None:
                setattr(room, key, fields[key])
        self.write_room_at(idx, room)
        return room

    def delete_room(self, room_id):
        idx, room = self.find_room_by_id(room_id)
        if room is None:
            raise ValueError(f"ไม่พบห้องรหัส {room_id}")
        active = [b for _, b in self.all_bookings()
                  if b.room_id == room_id and b.status == STATUS_BOOKED]
        if active:
            raise ValueError(f"ห้องนี้ยังมีการจอง Active อยู่ {len(active)} รายการ กรุณายกเลิกการจองก่อน")
        room.status = 0
        self.write_room_at(idx, room)
        return room

    # ---------------- bookings.dat ----------------
    def booking_count(self) -> int:
        return os.path.getsize(self.booking_path) // BOOKING_SIZE

    def read_booking_at(self, index: int):
        with open(self.booking_path, "rb") as f:
            f.seek(index * BOOKING_SIZE)
            raw = f.read(BOOKING_SIZE)
        if len(raw) != BOOKING_SIZE:
            return None
        try:
            return Booking.unpack(raw)
        except struct.error:
            return None

    def all_bookings(self):
        out = []
        for i in range(self.booking_count()):
            b = self.read_booking_at(i)
            if b is not None:
                out.append((i, b))
        return out

    def find_booking_by_id(self, booking_id: int):
        for i, b in self.all_bookings():
            if b.booking_id == booking_id:
                return i, b
        return None, None

    def find_free_booking_slot(self):
        for i, b in self.all_bookings():
            if b.status == STATUS_CANCELLED:
                return i
        return None

    def write_booking_at(self, index: int, booking: Booking):
        with open(self.booking_path, "r+b") as f:
            f.seek(index * BOOKING_SIZE)
            f.write(booking.pack())
            f.flush()
            os.fsync(f.fileno())

    def append_booking(self, booking: Booking):
        with open(self.booking_path, "ab") as f:
            f.write(booking.pack())
            f.flush()
            os.fsync(f.fileno())

    def next_booking_id(self) -> int:
        ids = [b.booking_id for _, b in self.all_bookings()]
        return (max(ids) + 1) if ids else 1

    def has_conflict(self, room_id, booking_date, start_time, end_time, exclude_booking_id=None):
        """คืน booking ที่ชนกันตัวแรกถ้ามี (เวลาซ้อนทับในห้อง/วันเดียวกัน และยัง Booked)"""
        new_s, new_e = time_to_minutes(start_time), time_to_minutes(end_time)
        for _, b in self.all_bookings():
            if b.status != STATUS_BOOKED:
                continue
            if b.room_id != room_id or b.booking_date != booking_date:
                continue
            if exclude_booking_id is not None and b.booking_id == exclude_booking_id:
                continue
            b_s, b_e = time_to_minutes(b.start_time), time_to_minutes(b.end_time)
            if new_s < b_e and b_s < new_e:  # overlap
                return b
        return None

    # ---------------- bookings.idx ----------------
    def idx_count(self) -> int:
        return os.path.getsize(self.idx_path) // IDX_SIZE

    def idx_find_slot(self, room_id: int):
        n = self.idx_count()
        with open(self.idx_path, "rb") as f:
            for i in range(n):
                f.seek(i * IDX_SIZE)
                raw = f.read(IDX_SIZE)
                if len(raw) != IDX_SIZE:
                    continue
                rid, _, _ = IDX_STRUCT.unpack(raw)
                if rid == room_id:
                    return i
        return None

    def idx_get(self, room_id: int):
        slot = self.idx_find_slot(room_id)
        if slot is None:
            return 0, None
        with open(self.idx_path, "rb") as f:
            f.seek(slot * IDX_SIZE)
            raw = f.read(IDX_SIZE)
        _, active_count, last_booking_id = IDX_STRUCT.unpack(raw)
        return active_count, last_booking_id

    def idx_set(self, room_id: int, active_count: int, last_booking_id: int):
        slot = self.idx_find_slot(room_id)
        raw = IDX_STRUCT.pack(room_id, active_count, last_booking_id)
        if slot is None:
            with open(self.idx_path, "ab") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
        else:
            with open(self.idx_path, "r+b") as f:
                f.seek(slot * IDX_SIZE)
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())

    def idx_recount_room(self, room_id: int, last_booking_id: int):
        """คำนวณจำนวนจอง Active ของห้องนี้ใหม่ทั้งหมด แล้วอัปเดต bookings.idx"""
        active_count = sum(1 for _, b in self.all_bookings()
                            if b.room_id == room_id and b.status == STATUS_BOOKED)
        self.idx_set(room_id, active_count, last_booking_id)

    # ---------------- High-level booking operations ----------------
    def add_booking(self, room_id, customer_name, booking_date, start_time, end_time):
        _, room = self.find_room_by_id(room_id)
        if room is None:
            raise ValueError(f"ไม่พบห้องรหัส {room_id} (หรือถูกลบไปแล้ว)")
        if end_time <= start_time:
            raise ValueError("เวลาเลิกต้องอยู่หลังเวลาเริ่ม")
        conflict = self.has_conflict(room_id, booking_date, start_time, end_time)
        if conflict is not None:
            raise ValueError(
                f"เวลาซ้อนทับกับการจอง #{conflict.booking_id} "
                f"({conflict.customer_name}, {conflict.time_range_str()})"
            )
        hours = (time_to_minutes(end_time) - time_to_minutes(start_time)) / 60.0
        total_price = round(hours * room.rate, 2)
        booking_id = self.next_booking_id()
        booking = Booking(booking_id, room_id, customer_name, booking_date, start_time,
                           end_time, hours, total_price, STATUS_BOOKED, int(time.time()))
        slot = self.find_free_booking_slot()
        if slot is not None:
            self.write_booking_at(slot, booking)
        else:
            self.append_booking(booking)
        self.idx_recount_room(room_id, booking_id)
        return booking

    def update_booking(self, booking_id, customer_name=None, booking_date=None,
                        start_time=None, end_time=None):
        idx, booking = self.find_booking_by_id(booking_id)
        if booking is None or booking.status != STATUS_BOOKED:
            raise ValueError(f"ไม่พบการจอง #{booking_id} ที่ยัง Active อยู่")
        _, room = self.find_room_by_id(booking.room_id)
        if room is None:
            raise ValueError("ห้องของการจองนี้ถูกลบไปแล้ว ไม่สามารถแก้ไขได้")

        new_date = booking_date if booking_date is not None else booking.booking_date
        new_start = start_time if start_time is not None else booking.start_time
        new_end = end_time if end_time is not None else booking.end_time
        if new_end <= new_start:
            raise ValueError("เวลาเลิกต้องอยู่หลังเวลาเริ่ม")
        conflict = self.has_conflict(booking.room_id, new_date, new_start, new_end,
                                      exclude_booking_id=booking_id)
        if conflict is not None:
            raise ValueError(
                f"เวลาซ้อนทับกับการจอง #{conflict.booking_id} "
                f"({conflict.customer_name}, {conflict.time_range_str()})"
            )
        if customer_name is not None:
            booking.customer_name = customer_name
        booking.booking_date = new_date
        booking.start_time = new_start
        booking.end_time = new_end
        booking.hours = (time_to_minutes(new_end) - time_to_minutes(new_start)) / 60.0
        booking.total_price = round(booking.hours * room.rate, 2)
        self.write_booking_at(idx, booking)
        self.idx_recount_room(booking.room_id, booking_id)
        return booking

    def cancel_booking(self, booking_id):
        idx, booking = self.find_booking_by_id(booking_id)
        if booking is None or booking.status != STATUS_BOOKED:
            raise ValueError(f"ไม่พบการจอง #{booking_id} ที่ยัง Active อยู่")
        booking.status = STATUS_CANCELLED
        self.write_booking_at(idx, booking)
        self.idx_recount_room(booking.room_id, booking_id)
        return booking


# ============================================================
# 5) INPUT VALIDATION HELPERS
# ============================================================

def input_int(prompt, min_val=None, max_val=None, allow_blank=False):
    while True:
        s = input(prompt).strip()
        if allow_blank and s == "":
            return None
        try:
            v = int(s)
        except ValueError:
            print("  [!] กรุณาใส่ตัวเลขจำนวนเต็มเท่านั้น")
            continue
        if min_val is not None and v < min_val:
            print(f"  [!] ค่าต้องไม่น้อยกว่า {min_val}")
            continue
        if max_val is not None and v > max_val:
            print(f"  [!] ค่าต้องไม่มากกว่า {max_val}")
            continue
        return v


def input_float(prompt, min_val=None, allow_blank=False):
    while True:
        s = input(prompt).strip()
        if allow_blank and s == "":
            return None
        try:
            v = float(s)
        except ValueError:
            print("  [!] กรุณาใส่ตัวเลขเท่านั้น")
            continue
        if min_val is not None and v < min_val:
            print(f"  [!] ค่าต้องไม่น้อยกว่า {min_val}")
            continue
        return v


def input_str(prompt, max_len, allow_blank=False):
    while True:
        s = input(prompt).strip()
        if s == "" and not allow_blank:
            print("  [!] ห้ามเว้นว่าง")
            continue
        if s and len(s.encode("utf-8")) > max_len:
            print(f"  [!] ข้อความยาวเกินไป (จำกัด {max_len} bytes) จะถูกตัดให้พอดี")
        return s if s != "" else None


def input_date(prompt, allow_blank=False):
    while True:
        s = input(prompt).strip()
        if allow_blank and s == "":
            return None
        v = parse_date(s)
        if v is None:
            print("  [!] รูปแบบวันที่ไม่ถูกต้อง กรุณาใส่ YYYY-MM-DD หรือ DD/MM/YYYY")
            continue
        return v


def input_time(prompt, allow_blank=False):
    while True:
        s = input(prompt).strip()
        if allow_blank and s == "":
            return None
        v = parse_time(s)
        if v is None:
            print("  [!] รูปแบบเวลาไม่ถูกต้อง กรุณาใส่ HH:MM เช่น 14:00")
            continue
        return v


def input_yes_no(prompt, allow_blank=False):
    while True:
        s = input(prompt).strip().lower()
        if allow_blank and s == "":
            return None
        if s in ("y", "yes", "1", "ใช่"):
            return 1
        if s in ("n", "no", "0", "ไม่"):
            return 0
        print("  [!] กรุณาตอบ y/n เท่านั้น")


def input_menu_choice(prompt, choices):
    while True:
        s = input(prompt).strip()
        if s in choices:
            return s
        print(f"  [!] กรุณาเลือกตัวเลือกที่ถูกต้อง ({'/'.join(choices)})")


# ============================================================
# 6) DISPLAY HELPERS
# ============================================================

def print_room_row(r: Room):
    print(f"  {r.room_id:<6} {r.name:<12} {r.type_:<12} cap={r.capacity:<3} floor={r.floor:<3} "
          f"rate={r.rate:>8.2f} thb/hr  [{r.status_text()}]")


def print_booking_row(b: Booking, room_name=""):
    print(f"  #{b.booking_id:<5} Room={b.room_id:<5} {room_name:<10} {b.customer_name:<20} "
          f"{b.date_str()} {b.time_range_str():<11} {b.hours:>4.1f}h  "
          f"{b.total_price:>9.2f} thb  [{b.status_text()}]")


def room_name_of(db: MusicRoomBookingDB, room_id: int) -> str:
    for _, r in db.all_rooms():
        if r.room_id == room_id:
            return r.name
    return "(ไม่พบห้อง)"


# ============================================================
# 7) REPORT GENERATION (report.txt)
# ============================================================

def generate_report(db: MusicRoomBookingDB, path=REPORT_FILE) -> str:
    now = datetime.now(TZ_BKK)
    rooms = db.all_rooms()
    bookings = db.all_bookings()

    lines = []
    lines.append("Music Room BOOKING System - Summary Report")
    lines.append(f"Generated At : {now.strftime('%Y-%m-%d %H:%M:%S')} (+07:00)")
    lines.append(f"App Version : {APP_VERSION}")
    lines.append("Endianness : Little-Endian")
    lines.append("Encoding : UTF-8 (fixed-length)")
    lines.append("")

    # --- Rooms table ---
    lines.append("=== Rooms (rooms.dat) ===")
    lines.append(f"{'RoomID':<8}{'Name':<14}{'Type':<14}{'Cap':<6}{'Floor':<7}{'Rate/hr':<10}{'Status':<9}")
    active_rooms = 0
    deleted_rooms = 0
    for _, r in sorted(rooms, key=lambda x: x[1].room_id):
        lines.append(f"{r.room_id:<8}{r.name:<14}{r.type_:<14}{r.capacity:<6}{r.floor:<7}"
                      f"{r.rate:<10.2f}{r.status_text():<9}")
        if r.status == 1:
            active_rooms += 1
        else:
            deleted_rooms += 1
    lines.append(f"* Total Rooms : {len(rooms)}   Active : {active_rooms}   Deleted : {deleted_rooms}")
    lines.append("")

    # --- Booking stats ---
    active_bookings = [b for _, b in bookings if b.status == STATUS_BOOKED]
    cancelled_bookings = [b for _, b in bookings if b.status == STATUS_CANCELLED]
    today_int = int(now.strftime("%Y%m%d"))
    todays_active = [b for b in active_bookings if b.booking_date == today_int]
    total_revenue = sum(b.total_price for b in active_bookings)

    lines.append("=== Booking Summary (bookings.dat) ===")
    lines.append(f"* Total Booking Records : {len(bookings)}")
    lines.append(f"* Active (Booked) : {len(active_bookings)}")
    lines.append(f"* Cancelled : {len(cancelled_bookings)}")
    lines.append(f"* Bookings Today ({fmt_date(today_int)}) : {len(todays_active)}")
    lines.append(f"* Total Revenue (Active bookings) : {total_revenue:,.2f} thb")
    lines.append("")

    # --- Revenue / bookings by room ---
    lines.append("=== Bookings by Room (Active only) ===")
    by_room = {}
    for b in active_bookings:
        by_room.setdefault(b.room_id, {"count": 0, "revenue": 0.0})
        by_room[b.room_id]["count"] += 1
        by_room[b.room_id]["revenue"] += b.total_price
    if by_room:
        for room_id, agg in sorted(by_room.items(), key=lambda x: -x[1]["revenue"]):
            lines.append(f"* {room_name_of(db, room_id):<14} (Room {room_id}) : "
                          f"{agg['count']} booking(s), {agg['revenue']:,.2f} thb")
    else:
        lines.append("* (ยังไม่มีการจองที่ Active)")
    lines.append("")

    # --- Recent bookings (history) ---
    lines.append("=== Recent Bookings (last entries, newest first) ===")
    recent = sorted(bookings, key=lambda x: x[1].created_ts, reverse=True)[:10]
    if recent:
        for _, b in recent:
            created_str = datetime.fromtimestamp(b.created_ts, TZ_BKK).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"* [{created_str}] #{b.booking_id} {room_name_of(db, b.room_id)} "
                         f"({b.room_id}) - {b.customer_name} on {b.date_str()} {b.time_range_str()} "
                         f"= {b.total_price:.2f} thb [{b.status_text()}]")
    else:
        lines.append("* (ยังไม่มีประวัติการจอง)")

    content = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return content


# ============================================================
# 8) SAMPLE / DEMO DATA
# ============================================================

DEMO_ROOMS = [
    (1001, "Studio A", "Small Band", 4, 1, 300.00),
    (1002, "Studio B", "Small Band", 5, 1, 350.00),
    (1003, "Studio C", "Full Band", 8, 1, 500.00),
    (1004, "Studio D", "Full Band", 10, 2, 600.00),
    (1005, "VIP Room", "Premium", 12, 2, 1000.00),
    (1006, "Vocal Box", "Solo/Vocal", 2, 2, 150.00),
    (1007, "Piano Room", "Solo/Duo", 2, 3, 200.00),
    (1008, "Drum Room", "Practice", 2, 3, 200.00),
    (1009, "Studio E", "Full Band", 8, 3, 500.00),
]


def seed_demo_data(db: MusicRoomBookingDB):
    added_rooms, skipped_rooms = 0, 0
    for room_id, name, type_, cap, floor, rate in DEMO_ROOMS:
        try:
            db.add_room(room_id, name, type_, cap, floor, rate)
            added_rooms += 1
        except ValueError:
            skipped_rooms += 1

    today = date.today()
    tomorrow_int = int((today.replace(day=today.day)).strftime("%Y%m%d")) if False else None
    d1 = int(today.strftime("%Y%m%d"))
    d2 = int((today.replace(day=today.day)).strftime("%Y%m%d"))  # today, kept simple/portable

    demo_bookings = [
        (1002, "คุณสมชาย ใจดี", d1, "14:00", "16:00"),
        (1005, "วง The Sunset", d1, "18:00", "21:00"),
        (1006, "คุณมานี ร้องเพลง", d1, "10:00", "11:00"),
        (1003, "วง Indy Kids", d1, "13:00", "15:00"),
    ]
    added_bookings, skipped_bookings = 0, 0
    for room_id, cust, bdate, stime, etime in demo_bookings:
        try:
            db.add_booking(room_id, cust, bdate, parse_time(stime), parse_time(etime))
            added_bookings += 1
        except ValueError:
            skipped_bookings += 1

    print(f"  -> ห้อง: เพิ่ม {added_rooms} รายการ (ข้าม {skipped_rooms})")
    print(f"  -> การจอง: เพิ่ม {added_bookings} รายการ (ข้าม {skipped_bookings})")


# ============================================================
# 9) CLI MENU
# ============================================================

def show_active_rooms(db: MusicRoomBookingDB):
    rooms = [(i, r) for i, r in db.all_rooms() if r.status == 1]
    if not rooms:
        print("  (ยังไม่มีห้องในระบบ ลองเมนู 7 เพื่อโหลดข้อมูลตัวอย่าง)")
        return
    print("  ห้องที่เปิดให้บริการ:")
    for _, r in sorted(rooms, key=lambda x: x[1].room_id):
        print_room_row(r)


def menu_add_booking(db: MusicRoomBookingDB):
    print("\n--- จองห้องดนตรี (Add Booking) ---")
    show_active_rooms(db)
    room_id = input_int("  ระบุ RoomID ที่ต้องการจอง: ", min_val=1)
    customer_name = input_str(f"  ชื่อลูกค้า (สูงสุด {CUSTOMER_LEN} bytes): ", CUSTOMER_LEN)
    booking_date = input_date("  วันที่จอง (YYYY-MM-DD หรือ DD/MM/YYYY): ")
    start_time = input_time("  เวลาเริ่ม (HH:MM): ")
    end_time = input_time("  เวลาเลิก (HH:MM): ")
    try:
        booking = db.add_booking(room_id, customer_name, booking_date, start_time, end_time)
        print(f"  [OK] จองสำเร็จ! เลขที่การจอง #{booking.booking_id} "
              f"รวม {booking.hours:.1f} ชม. เป็นเงิน {booking.total_price:,.2f} บาท")
    except ValueError as e:
        print(f"  [!] {e}")


def menu_update_booking(db: MusicRoomBookingDB):
    print("\n--- แก้ไขการจอง (Update Booking) ---")
    booking_id = input_int("  ระบุเลขที่การจอง (Booking ID): ", min_val=1)
    idx, booking = db.find_booking_by_id(booking_id)
    if booking is None:
        print(f"  [!] ไม่พบการจอง #{booking_id}")
        return
    print("  ข้อมูลปัจจุบัน:")
    print_booking_row(booking, room_name_of(db, booking.room_id))
    if booking.status != STATUS_BOOKED:
        print("  [!] การจองนี้ถูกยกเลิกไปแล้ว ไม่สามารถแก้ไขได้")
        return
    print("  (เว้นว่างหากไม่ต้องการแก้ไขฟิลด์นั้น)")
    customer_name = input_str(f"  ชื่อลูกค้าใหม่ [{booking.customer_name}]: ", CUSTOMER_LEN, allow_blank=True)
    booking_date = input_date(f"  วันที่ใหม่ [{booking.date_str()}]: ", allow_blank=True)
    start_time = input_time(f"  เวลาเริ่มใหม่ [{fmt_time(booking.start_time)}]: ", allow_blank=True)
    end_time = input_time(f"  เวลาเลิกใหม่ [{fmt_time(booking.end_time)}]: ", allow_blank=True)
    try:
        updated = db.update_booking(booking_id, customer_name=customer_name, booking_date=booking_date,
                                     start_time=start_time, end_time=end_time)
        print(f"  [OK] อัปเดตการจอง #{booking_id} เรียบร้อยแล้ว:")
        print_booking_row(updated, room_name_of(db, updated.room_id))
    except ValueError as e:
        print(f"  [!] {e}")


def menu_cancel_booking(db: MusicRoomBookingDB):
    print("\n--- ยกเลิกการจอง (Cancel Booking) ---")
    booking_id = input_int("  ระบุเลขที่การจองที่ต้องการยกเลิก: ", min_val=1)
    idx, booking = db.find_booking_by_id(booking_id)
    if booking is None or booking.status != STATUS_BOOKED:
        print(f"  [!] ไม่พบการจอง #{booking_id} ที่ยัง Active อยู่")
        return
    print_booking_row(booking, room_name_of(db, booking.room_id))
    confirm = input_yes_no("  ยืนยันการยกเลิก? (y/n): ")
    if confirm == 1:
        db.cancel_booking(booking_id)
        print(f"  [OK] ยกเลิกการจอง #{booking_id} เรียบร้อยแล้ว")
    else:
        print("  ยกเลิกคำสั่ง (ไม่ได้ยกเลิกการจอง)")


def menu_view_booking(db: MusicRoomBookingDB):
    while True:
        print("\n--- ดูการจอง (View Booking) ---")
        print("  1) ดูรายการเดียว (ตาม Booking ID)")
        print("  2) ดูทั้งหมด")
        print("  3) ดูแบบกรอง")
        print("  4) สถิติโดยสรุป")
        print("  0) กลับเมนูหลัก")
        choice = input_menu_choice("  เลือก: ", ["0", "1", "2", "3", "4"])

        if choice == "0":
            return
        elif choice == "1":
            booking_id = input_int("  ระบุ Booking ID: ", min_val=1)
            _, b = db.find_booking_by_id(booking_id)
            if b is None:
                print(f"  [!] ไม่พบการจอง #{booking_id}")
            else:
                print_booking_row(b, room_name_of(db, b.room_id))
        elif choice == "2":
            bookings = db.all_bookings()
            if not bookings:
                print("  (ยังไม่มีข้อมูลการจอง)")
            for _, b in sorted(bookings, key=lambda x: (x[1].booking_date, x[1].start_time)):
                print_booking_row(b, room_name_of(db, b.room_id))
        elif choice == "3":
            print("  กรองตาม: 1) RoomID  2) วันที่  3) สถานะ  4) ชื่อลูกค้า")
            f_choice = input_menu_choice("  เลือกเงื่อนไข: ", ["1", "2", "3", "4"])
            bookings = [b for _, b in db.all_bookings()]
            if f_choice == "1":
                val = input_int("  ใส่ RoomID: ", min_val=1)
                filtered = [b for b in bookings if b.room_id == val]
            elif f_choice == "2":
                val = input_date("  ใส่วันที่ (YYYY-MM-DD หรือ DD/MM/YYYY): ")
                filtered = [b for b in bookings if b.booking_date == val]
            elif f_choice == "3":
                val = input_menu_choice("  Booked(1) หรือ Cancelled(0): ", ["0", "1"])
                filtered = [b for b in bookings if b.status == int(val)]
            else:
                val = input_str("  ใส่ชื่อลูกค้า (ค้นหาบางส่วนได้): ", CUSTOMER_LEN)
                filtered = [b for b in bookings if val.lower() in b.customer_name.lower()]
            if not filtered:
                print("  (ไม่พบข้อมูลที่ตรงเงื่อนไข)")
            for b in sorted(filtered, key=lambda x: (x.booking_date, x.start_time)):
                print_booking_row(b, room_name_of(db, b.room_id))
        elif choice == "4":
            bookings = [b for _, b in db.all_bookings()]
            active = [b for b in bookings if b.status == STATUS_BOOKED]
            cancelled = [b for b in bookings if b.status == STATUS_CANCELLED]
            revenue = sum(b.total_price for b in active)
            print(f"  Total: {len(bookings)}  Booked: {len(active)}  Cancelled: {len(cancelled)}")
            print(f"  Total Revenue (Active): {revenue:,.2f} thb")
            if active:
                busiest = max(
                    {b.room_id for b in active},
                    key=lambda rid: sum(1 for b in active if b.room_id == rid)
                )
                cnt = sum(1 for b in active if b.room_id == busiest)
                print(f"  ห้องที่ถูกจองมากที่สุด: {room_name_of(db, busiest)} (Room {busiest}) - {cnt} ครั้ง")


def menu_room_management(db: MusicRoomBookingDB):
    while True:
        print("\n--- จัดการห้องดนตรี (Room Master Data) ---")
        print("  1) Add     เพิ่มห้อง")
        print("  2) Update  แก้ไขห้อง")
        print("  3) Delete  ลบห้อง")
        print("  4) View    ดูห้องทั้งหมด")
        print("  0) กลับเมนูหลัก")
        choice = input_menu_choice("  เลือก: ", ["0", "1", "2", "3", "4"])

        if choice == "0":
            return
        elif choice == "1":
            room_id = input_int("  RoomID (ตัวเลข): ", min_val=1)
            _, existing = db.find_room_by_id(room_id)
            if existing is not None:
                print(f"  [!] มีห้องรหัส {room_id} อยู่แล้ว")
                continue
            name = input_str(f"  ชื่อห้อง (สูงสุด {NAME_LEN} bytes): ", NAME_LEN)
            type_ = input_str(f"  ประเภทห้อง (สูงสุด {TYPE_LEN} bytes): ", TYPE_LEN)
            capacity = input_int("  ความจุ (คน): ", min_val=1)
            floor = input_int("  ชั้น: ", min_val=0)
            rate = input_float("  อัตราค่าบริการ (บาท/ชม.): ", min_val=0)
            try:
                db.add_room(room_id, name, type_, capacity, floor, rate)
                print(f"  [OK] เพิ่มห้อง {room_id} - {name} เรียบร้อยแล้ว")
            except ValueError as e:
                print(f"  [!] {e}")
        elif choice == "2":
            room_id = input_int("  ระบุ RoomID ที่ต้องการแก้ไข: ", min_val=1)
            _, room = db.find_room_by_id(room_id)
            if room is None:
                print(f"  [!] ไม่พบห้องรหัส {room_id}")
                continue
            print_room_row(room)
            print("  (เว้นว่างหากไม่ต้องการแก้ไขฟิลด์นั้น)")
            name = input_str(f"  ชื่อใหม่ [{room.name}]: ", NAME_LEN, allow_blank=True)
            type_ = input_str(f"  ประเภทใหม่ [{room.type_}]: ", TYPE_LEN, allow_blank=True)
            capacity = input_int(f"  ความจุใหม่ [{room.capacity}]: ", min_val=1, allow_blank=True)
            floor = input_int(f"  ชั้นใหม่ [{room.floor}]: ", min_val=0, allow_blank=True)
            rate = input_float(f"  อัตราใหม่ [{room.rate:.2f}]: ", min_val=0, allow_blank=True)
            try:
                updated = db.update_room(room_id, name=name, type_=type_, capacity=capacity,
                                          floor=floor, rate=rate)
                print("  [OK] อัปเดตแล้ว:")
                print_room_row(updated)
            except ValueError as e:
                print(f"  [!] {e}")
        elif choice == "3":
            room_id = input_int("  ระบุ RoomID ที่ต้องการลบ: ", min_val=1)
            _, room = db.find_room_by_id(room_id)
            if room is None:
                print(f"  [!] ไม่พบห้องรหัส {room_id}")
                continue
            print_room_row(room)
            confirm = input_yes_no("  ยืนยันการลบ? (y/n): ")
            if confirm == 1:
                try:
                    db.delete_room(room_id)
                    print(f"  [OK] ลบห้อง {room_id} เรียบร้อยแล้ว")
                except ValueError as e:
                    print(f"  [!] {e}")
            else:
                print("  ยกเลิกการลบ")
        elif choice == "4":
            rooms = db.all_rooms()
            if not rooms:
                print("  (ยังไม่มีข้อมูล)")
            for _, r in sorted(rooms, key=lambda x: x[1].room_id):
                print_room_row(r)


def menu_report(db: MusicRoomBookingDB):
    print("\n--- สร้างรายงาน (Generate Report) ---")
    content = generate_report(db)
    print(f"  [OK] สร้างรายงานที่ '{REPORT_FILE}' เรียบร้อยแล้ว\n")
    print(content)


def main():
    print("=" * 64)
    print(" ระบบจองห้องดนตรี (Music Room Booking System) v" + APP_VERSION)
    print("=" * 64)
    db = MusicRoomBookingDB()

    while True:
        print("\n================== MENU ==================")
        print(" 1) Add      จองห้อง (New Booking)")
        print(" 2) Update   แก้ไขการจอง")
        print(" 3) Delete   ยกเลิกการจอง")
        print(" 4) View     ดูการจอง")
        print(" 5) Generate Report (.txt)")
        print(" 6) จัดการห้องดนตรี (Room Master Data)")
        print(" 7) โหลดข้อมูลตัวอย่าง (Seed Demo Data)")
        print(" 0) Exit     ออกจากโปรแกรม")
        choice = input_menu_choice(" เลือกเมนู: ", ["0", "1", "2", "3", "4", "5", "6", "7"])

        if choice == "1":
            menu_add_booking(db)
        elif choice == "2":
            menu_update_booking(db)
        elif choice == "3":
            menu_cancel_booking(db)
        elif choice == "4":
            menu_view_booking(db)
        elif choice == "5":
            menu_report(db)
        elif choice == "6":
            menu_room_management(db)
        elif choice == "7":
            seed_demo_data(db)
        elif choice == "0":
            print("\n กำลังปิดโปรแกรม...")
            gen = input_yes_no(" ต้องการสร้างรายงานสรุปก่อนออกหรือไม่? (y/n): ")
            if gen == 1:
                generate_report(db)
                print(f" [OK] บันทึกรายงานที่ '{REPORT_FILE}' แล้ว")
            print(" ปิดโปรแกรมเรียบร้อย (flush/sync ไฟล์ทั้งหมดแล้ว) บ๊ายบาย!")
            break


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n\n [!] ตรวจพบการยกเลิกโปรแกรม - ปิดโปรแกรมอย่างปลอดภัย")
        sys.exit(0)
