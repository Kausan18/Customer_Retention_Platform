"""
app/db/seed.py

Creates the V1 schema (orders, order_items, shipments, payments,
product_catalog, returns, tickets, workflow_runs, action_proposals,
approvals, audit_events) in data/customer_support.sqlite3, and populates
it with synthetic data layered on top of the real customer IDs found in
customer_shopping_data.csv.

The CSV is read-only input. The existing `purchases` table (owned by
ShoppingDatabase in the base version) is never touched by this script.

Run:
    python -m app.db.seed

Idempotent: safe to re-run. New-table data is dropped and regenerated
each time; purchases/customers from the CSV are not altered.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent.parent  # project root
DATA_DIR = ROOT_DIR / "data"
CSV_PATH = DATA_DIR / "customer_shopping_data.csv" / "customer_shopping_data.csv"
DB_PATH = DATA_DIR / "customer_support.sqlite3"

SEED = 42
NUM_ORDERS = 200
RETURN_RATE = 0.30  # fraction of delivered orders that also get a return
CURRENCY = "INR"

CATEGORIES = ["Electronics", "Clothing", "Cosmetics", "Food", "Shoes", "Toys"]

# Prices are in minor units (paise): 1 INR = 100 paise.
PRODUCT_DATA = [
    ("Electronics", [
        ("Sony WH-1000XM5 Headphones", 3299900),
        ("Apple AirPods Pro 2nd Generation", 2499900),
        ("Samsung Galaxy S24 256GB", 7499900),
        ("Dell Inspiron 14 Laptop", 5799900),
        ("LG 55-inch OLED 4K Smart TV", 7999900),
        ("JBL Flip 6 Portable Bluetooth Speaker", 1199900),
        ("Logitech MX Master 3S Wireless Mouse", 899900),
        ("Canon EOS R50 Mirrorless Camera", 6499900),
        ("Samsung Galaxy Tab S9 FE", 3699900),
        ("OnePlus Nord CE 4 5G", 2499900),
        ("Apple Watch SE GPS 40mm", 2499000),
        ("Bose QuietComfort Ultra Earbuds", 2499000),
        ("Xiaomi 20000mAh Power Bank", 199900),
        ("TP-Link Archer AX55 Wi-Fi 6 Router", 799900),
        ("GoPro HERO12 Black Action Camera", 3999900),
        ("Lenovo IdeaPad Slim 3 Laptop", 4299900),
        ("HP Pavilion Plus 14 Laptop", 7299900),
        ("Sony Bravia 43-inch 4K Google TV", 4999900),
        ("Samsung 1TB Portable SSD T7 Shield", 1099900),
        ("Anker Soundcore Motion X600 Speaker", 1499900),
        ("Apple iPad Air 11-inch 128GB", 5999000),
        ("Amazon Echo Dot 5th Gen", 549900),
        ("Nothing Phone (2a) 256GB", 2799900),
        ("ASUS Vivobook 15 OLED Laptop", 6999900),
        ("Dyson V8 Absolute Cordless Vacuum", 3499900),
        ("Philips Airfryer HD9252 4.1L", 899900),
        ("Samsung Galaxy Buds FE", 799900),
        ("Realme Pad 2 128GB", 1999900),
        ("Epson EcoTank L3250 Wi-Fi Printer", 1499900),
        ("Fujifilm Instax Mini 12 Camera", 799900),
        ("Garmin Forerunner 255 GPS Watch", 2999900),
        ("OnePlus Buds Pro 2", 1199900),
        ("LG 1.5 Ton 5-Star Dual Inverter AC", 4999900),
        ("Godrej 236L Frost-Free Refrigerator", 2899900),
        ("Whirlpool 7kg Front Load Washing Machine", 3199900),
        ("Bajaj Majesty 2000W Room Heater", 499900),
        ("Tefal Easy Fry XL Air Fryer", 1299900),
        ("Marshall Emberton II Bluetooth Speaker", 1699900),
        ("BenQ GW2780 27-inch IPS Monitor", 1399900),
        ("Acer Nitro V 15 Gaming Laptop", 7499900),
        ("Samsung Galaxy A35 5G 128GB", 2799900),
        ("Motorola Edge 50 Fusion 256GB", 2499900),
        ("Xiaomi Smart Band 8 Pro", 699900),
        ("HyperX Cloud III Gaming Headset", 899900),
        ("Razer BlackWidow V4 X Gaming Keyboard", 1199900),
        ("Western Digital 2TB My Passport Drive", 699900),
        ("Seagate 5TB Expansion Portable Drive", 1299900),
        ("DJI Osmo Mobile 6 Smartphone Gimbal", 1499900),
        ("Nikon Z30 Mirrorless Camera Kit", 6499900),
        ("Samsung 32-inch Smart HD TV", 1499900),
    ]),
    ("Clothing", [
        ("Levi's 511 Slim Fit Men's Jeans", 299900),
        ("Fabindia Women's Handwoven Cotton Kurta", 249900),
        ("Allen Solly Men's Slim Fit Formal Shirt", 179900),
        ("Biba Women's Printed Anarkali Suit", 329900),
        ("Peter England Men's Cotton Chinos", 199900),
        ("H&M Women's Rib-Knit Midi Dress", 229900),
        ("Manyavar Men's Silk Blend Kurta Set", 499900),
        ("W for Woman Straight Fit Trousers", 189900),
        ("Van Heusen Men's Cotton Polo T-Shirt", 149900),
        ("Zara Women's Satin Effect Blouse", 299900),
        ("U.S. Polo Assn. Men's Crew Neck T-Shirt", 129900),
        ("Aurelia Women's Floral Printed Kurta", 159900),
        ("Jockey Men's Cotton Track Pants", 99900),
        ("Max Women's High-Rise Skinny Jeans", 119900),
        ("Roadster Men's Checked Casual Shirt", 99900),
        ("Sangria Women's Embroidered Palazzo Set", 279900),
        ("Adidas Essentials 3-Stripes Track Jacket", 349900),
        ("Puma Men's Active Training Shorts", 129900),
        ("Nike Dri-FIT Women's Running Tights", 249900),
        ("Decathlon Quechua Hiking Fleece Jacket", 499900),
        ("Raymond Men's Slim Fit Blazer", 499900),
        ("Libas Women's Printed Cotton Saree", 229900),
        ("Spykar Men's Regular Fit Denim Jeans", 219900),
        ("Levi's Women's 312 Shaping Slim Jeans", 329900),
        ("Indian Terrain Men's Linen Blend Shirt", 249900),
        ("Global Desi Women's Printed Maxi Dress", 279900),
        ("Cottonworld Men's Organic Cotton Henley", 119900),
        ("AND Women's Tailored Formal Trousers", 259900),
        ("Puma Women's Essentials Logo Hoodie", 299900),
        ("Campus Sutra Men's Bomber Jacket", 399900),
        ("Soch Women's Chanderi Silk Kurta", 449900),
        ("Bewakoof Men's Graphic Cotton T-Shirt", 79900),
        ("Wrogn Men's Printed Casual Shirt", 179900),
        ("Marks & Spencer Women's Cotton Nightdress", 249900),
        ("Clovia Women's Floral Cotton Night Suit", 199900),
        ("Beyoung Men's Oversized Plain T-Shirt", 69900),
        ("Sassafras Women's High-Waist Wide Leg Jeans", 199900),
        ("The Souled Store Marvel Graphic Sweatshirt", 199900),
        ("Vero Moda Women's Tie-Waist Shirt Dress", 399900),
        ("Arrow Men's Textured Formal Waistcoat", 349900),
        ("Kraus Jeans Men's Stretchable Denim Jacket", 299900),
        ("Jaipur Kurti Women's Cotton Palazzo Pants", 99900),
        ("Indo Era Men's Printed Nehru Jacket", 299900),
        ("Dixcy Scott Men's Cotton Vest Pack of 3", 59900),
        ("Zivame Women's Cotton Lounge Shorts", 79900),
        ("The Chennai Silks Women's Kanjivaram Saree", 499900),
        ("Rare Rabbit Men's Slim Fit Linen Shirt", 399900),
        ("Cazibe Women's Embroidered Straight Kurta", 149900),
        ("Snitch Men's Relaxed Fit Cargo Trousers", 249900),
        ("Monte Carlo Men's Cotton V-Neck Sweater", 499900),
    ]),
    ("Cosmetics", [
        ("Maybelline Fit Me Matte + Poreless Foundation", 54900),
        ("Lakme 9to5 Primer + Matte Lip Color", 69900),
        ("L'Oreal Paris Revitalift Hyaluronic Acid Serum", 89900),
        ("Cetaphil Moisturizing Cream 250g", 69900),
        ("Neutrogena Ultra Sheer Dry-Touch Sunscreen SPF 50+", 59900),
        ("The Ordinary Niacinamide 10% + Zinc 1% Serum", 75000),
        ("MAC Ruby Woo Retro Matte Lipstick", 210000),
        ("Nivea Soft Light Moisturizer 200ml", 29900),
        ("Dove Deep Moisture Body Wash 1L", 39900),
        ("Mamaearth Onion Hair Oil 150ml", 39900),
        ("Plum Green Tea Pore Cleansing Face Wash", 34500),
        ("Minimalist 10% Vitamin C Face Serum", 69900),
        ("Sugar Cosmetics Matte As Hell Crayon Lipstick", 79900),
        ("Maybelline Lash Sensational Mascara", 49900),
        ("Colorbar Perfect Match Primer", 89900),
        ("Biotique Bio Papaya Revitalizing Tan Removal Scrub", 19900),
        ("Lotus Herbals Safe Sun UV Screen Matte Gel SPF 50", 39900),
        ("Kama Ayurveda Rose Jasmine Face Cleanser", 89500),
        ("Forest Essentials Facial Cleanser Kashmiri Saffron", 119500),
        ("Nykaa So Creme Creamy Matte Lipstick", 39900),
        ("Faces Canada Ultime Pro Makeup Fixer", 69900),
        ("Himalaya Herbals Purifying Neem Face Wash", 18000),
        ("Garnier Micellar Cleansing Water 400ml", 39900),
        ("Re'equil Ultra Matte Dry Touch Sunscreen Gel", 69500),
        ("WOW Skin Science Aloe Vera Face Gel", 29900),
        ("The Face Shop Rice Water Bright Cleansing Foam", 49900),
        ("Clinique Happy Eau de Parfum 50ml", 299900),
        ("Engage W2 Perfume Spray for Women", 19900),
        ("Skinn by Titan Raw Eau de Parfum 50ml", 169900),
        ("Bella Vita Luxury CEO Man Eau de Parfum", 59900),
        ("BBlunt Intense Moisture Shampoo 400ml", 39900),
        ("L'Oreal Paris Extraordinary Oil Serum", 39900),
        ("Tresemme Keratin Smooth Conditioner 190ml", 19900),
        ("Indulekha Bringha Hair Oil 100ml", 34900),
        ("The Body Shop British Rose Body Yogurt", 69500),
        ("Bath & Body Works Japanese Cherry Blossom Mist", 169900),
        ("Nykaa Naturals Tea Tree Scalp Scrub", 49900),
        ("Ponds Super Light Gel Oil-Free Moisturizer", 19900),
        ("Olay Regenerist Micro-Sculpting Cream 50g", 89900),
        ("Kiehl's Ultra Facial Cream 50ml", 289500),
        ("Swiss Beauty Liquid Concealer", 24900),
        ("PAC Spotlight Liquid Foundation", 89500),
        ("Kay Beauty Matteinee Matte Lipstick", 119900),
        ("Colorbar Velvet Matte Nail Enamel", 19900),
        ("Lakme Eyeconic Kajal Twin Pack", 29900),
        ("Sunscoop Invisible Sunscreen SPF 40", 49900),
        ("Pilgrim Red Vine Face Cream", 49500),
        ("Aqualogica Glow+ Dewy Sunscreen SPF 50", 39900),
        ("Dot & Key Cica Calming Night Gel", 49500),
        ("Good Vibes Rosehip Radiant Glow Face Serum", 39900),
    ]),
    ("Food", [
        ("Tata Sampann Unpolished Toor Dal 1kg", 18900),
        ("India Gate Classic Basmati Rice 5kg", 69900),
        ("Amul Dark Chocolate 55% Cocoa 150g", 14900),
        ("Aashirvaad Whole Wheat Atta 5kg", 29900),
        ("MTR Ready-to-Eat Paneer Butter Masala 300g", 12900),
        ("Organic India Tulsi Green Tea 25 Tea Bags", 19900),
        ("Happilo Premium California Almonds 500g", 59900),
        ("Borges Extra Virgin Olive Oil 500ml", 54900),
        ("Kissan Mixed Fruit Jam 500g", 16900),
        ("Yoga Bar Protein Bars Variety Pack of 6", 44900),
        ("Dabur Honey Squeezy Pack 500g", 19900),
        ("Kellogg's Corn Flakes Original 875g", 27900),
        ("Nescafe Classic Instant Coffee 100g", 34900),
        ("Tata Tea Gold Black Tea 1kg", 49900),
        ("Patanjali Cow Ghee 1L", 59900),
        ("Fortune Sunlite Refined Sunflower Oil 5L", 69900),
        ("Madhur Pure & Hygienic Sugar 5kg", 24900),
        ("Britannia Good Day Cashew Cookies 600g", 14900),
        ("Parle-G Original Gluco Biscuits Family Pack", 10000),
        ("Amul Taaza Toned Milk 1L", 6900),
        ("Mother Dairy Classic Curd 400g", 5500),
        ("Epigamia Natural Greek Yogurt 90g", 5500),
        ("iD Fresh Malabar Parota 400g", 11000),
        ("Haldiram's Aloo Bhujia 400g", 9900),
        ("Bikaji Bikaneri Bhujia 400g", 11900),
        ("Paper Boat Aamras 3-Pack", 12000),
        ("Raw Pressery Cold Pressed Orange Juice 1L", 19900),
        ("Amul Processed Cheese Slices 200g", 13500),
        ("Milky Mist Paneer 200g", 11000),
        ("Tata Sampann Masoor Dal 1kg", 14900),
        ("Tata Sampann Moong Dal 1kg", 17900),
        ("24 Mantra Organic Quinoa 500g", 24900),
        ("True Elements Rolled Oats 1kg", 22900),
        ("Saffola Masala Oats Classic Masala 500g", 16900),
        ("MTR Rava Idli Breakfast Mix 500g", 12500),
        ("Gits Gulab Jamun Dessert Mix 200g", 8900),
        ("Mapro Strawberry Crush 750ml", 22900),
        ("Veeba Eggless Mayonnaise 250g", 9900),
        ("Wingreens Farms Schezwan Chilli Sauce 200g", 9500),
        ("Urban Platter Dark Chocolate Chips 250g", 24900),
        ("Amul Gold Full Cream Milk 1L", 7500),
        ("Britannia The Laughing Cow Cheese Spread 200g", 13900),
        ("Kissan Fresh Tomato Ketchup 1kg", 14900),
        ("Ching's Secret Hakka Noodles 600g", 9900),
        ("Sunfeast Yippee Magic Masala Noodles 4-Pack", 5600),
        ("MTR Instant Gulab Jamun 500g", 17900),
        ("Sundrop Peanut Butter Crunchy 1kg", 29900),
        ("Bagrry's Crunchy Muesli Fruit and Nut 700g", 34900),
        ("Aashirvaad Multigrain Atta 5kg", 35900),
        ("Catch Sprinkler Table Salt 200g", 5000),
    ]),
    ("Shoes", [
        ("Nike Air Max 270 Men's Running Shoes", 799900),
        ("Adidas Ultraboost Light Running Shoes", 799900),
        ("Puma Suede Classic XXI Sneakers", 699900),
        ("Bata Power Men's Sports Shoes", 199900),
        ("Crocs Classic Clog Unisex", 399900),
        ("Skechers Go Walk Flex Slip-On Shoes", 599900),
        ("Woodland Men's Leather Trekking Boots", 329900),
        ("Converse Chuck Taylor All Star High Tops", 599900),
        ("Clarks Un Costa Lace Leather Oxford Shoes", 799900),
        ("Birkenstock Arizona EVA Sandals", 499900),
        ("Reebok Floatride Energy 5 Running Shoes", 799900),
        ("Asics Gel-Contend 8 Running Shoes", 649900),
        ("New Balance 574 Core Sneakers", 799900),
        ("Under Armour Charged Assert 10 Running Shoes", 799900),
        ("Red Tape Men's Casual Lace-Up Sneakers", 249900),
        ("Campus Men's Oxyfit Running Shoes", 129900),
        ("Sparx Men's Mesh Running Shoes", 99900),
        ("Liberty Warrior Men's Sports Shoes", 149900),
        ("Relaxo Flite Women's Printed Flip-Flops", 59900),
        ("Paragon Men's Formal Leather Shoes", 129900),
        ("Metro Women's Block Heel Sandals", 99900),
        ("Mochi Women's Embellished Flat Sandals", 149900),
        ("Inc.5 Women's Pointed Toe Ballet Flats", 129900),
        ("Catwalk Women's Strappy High-Heel Sandals", 199900),
        ("Hush Puppies Men's Leather Loafers", 799900),
        ("Lee Cooper Men's Canvas Lace-Up Sneakers", 149900),
        ("Fila Disruptor II Premium Sneakers", 499900),
        ("ASICS Gel-Kayano 30 Running Shoes", 799900),
        ("Nike Revolution 7 Road Running Shoes", 449900),
        ("Adidas Runfalcon 3.0 Running Shoes", 399900),
        ("Puma Flyer Runner Engineered Knit Shoes", 299900),
        ("Skechers D'Lites Fresh Start Sneakers", 599900),
        ("Decathlon Quechua NH150 Hiking Shoes", 199900),
        ("Decathlon Kiprun Jogflow 500.1 Running Shoes", 249900),
        ("Red Chief Men's Genuine Leather Derby Shoes", 299900),
        ("Bata Women's Comfit Slip-On Shoes", 119900),
        ("Khadim's Women's Faux Leather Ballet Flats", 89900),
        ("Walkaroo Men's Lightweight EVA Clogs", 79900),
        ("Vans Old Skool Classic Skate Shoes", 699900),
        ("Jordan Air 1 Low Men's Sneakers", 799900),
        ("Nike Court Vision Low Next Nature Sneakers", 599900),
        ("Adidas Grand Court 2.0 Tennis Shoes", 499900),
        ("Puma Caven 2.0 Retro Sneakers", 449900),
        ("New Balance Fresh Foam Arishi V4", 699900),
        ("Reebok Classic Leather Sneakers", 599900),
        ("Crocs Bayaband Clog Unisex", 499900),
        ("Birkenstock Gizeh EVA Thong Sandals", 399900),
        ("Woodland Women's Suede Outdoor Sandals", 249900),
        ("Metro Men's Double Monk Strap Formal Shoes", 399900),
        ("Mochi Men's Penny Loafers", 249900),
    ]),
    ("Toys", [
        ("LEGO City Police Station Building Set 60316", 499900),
        ("LEGO Star Wars X-Wing Fighter Set 75355", 499900),
        ("Mattel Barbie Dreamhouse Dollhouse", 499900),
        ("Hasbro Nerf Elite 2.0 Eaglepoint RD-8 Blaster", 299900),
        ("Hot Wheels City Ultimate Garage Playset", 499900),
        ("Fisher-Price Laugh & Learn Smart Stages Puppy", 399900),
        ("Melissa & Doug Wooden Building Blocks 100-Piece Set", 249900),
        ("Ravensburger GraviTrax Starter Set", 499900),
        ("Play-Doh Kitchen Creations Ice Cream Truck", 499900),
        ("Monopoly Classic Board Game", 299900),
        ("LEGO Technic McLaren Formula 1 Race Car 42141", 499900),
        ("LEGO Friends Heartlake City Community Center", 499900),
        ("Barbie Dream Besties Malibu Doll", 249900),
        ("Hot Wheels 20-Car Gift Pack", 99900),
        ("Nerf N Series Agility Blaster", 199900),
        ("Hasbro Gaming Cluedo Classic Board Game", 299900),
        ("Funskool Jenga Classic Wooden Block Game", 149900),
        ("Smartivity Hydraulic Plane Launcher STEM Kit", 79900),
        ("Imagimake Mapology India Map Puzzle", 49900),
        ("Skillmatics Guess in 10 Animal Planet Card Game", 29900),
        ("Toyshine Musical Baby Piano Keyboard", 49900),
        ("Webby Remote Control Monster Truck", 79900),
        ("Hamleys Plush Teddy Bear 30cm", 59900),
        ("Baybee Musical Activity Walker for Toddlers", 149900),
        ("Chicco Eco+ Turbo Team Racing Car", 49900),
        ("Lego Classic Medium Creative Brick Box 10696", 399900),
        ("Playmobil City Life School Bus", 249900),
        ("Hot Wheels Track Builder Unlimited Loop Kit", 499900),
        ("Mattel Uno Card Game", 20000),
        ("Rubik's 3x3 Speed Cube", 24900),
        ("Frank Dominoes Double-Six Set", 20000),
        ("Paw Patrol Chase Rescue Cruiser Vehicle", 39900),
        ("Disney Frozen Elsa Singing Doll", 199900),
        ("Hasbro My Little Pony Rainbow Dash Figure", 49900),
        ("Barbie Fashionistas Doll with Floral Dress", 79900),
        ("Nerf Elite 2.0 Commander RD-6 Blaster", 149900),
        ("Fisher-Price Rock-a-Stack Classic Toy", 24900),
        ("Skillofun Wooden Shape Sorter Cube", 69900),
        ("Shumee Wooden Animal Jigsaw Puzzle", 39900),
        ("Frank 100-Piece Wildlife Jigsaw Puzzle", 24900),
        ("Toy Imagine Wooden Kitchen Pretend Play Set", 299900),
        ("Little's Junior Ring Stacker Toy", 24900),
        ("Webby Kids Bowling Set with Pins and Ball", 29900),
        ("Storio Magnetic Chess and Checkers Travel Set", 39900),
        ("GIGGLES Baby Rattle and Teether Gift Set", 24900),
        ("Hamleys RC Racing Sports Car", 99900),
        ("Maisto Fresh Metal Die-Cast Lamborghini", 29900),
        ("R for Rabbit Tiny Toes Musical Toy Train", 99900),
        ("Skillmatics Preschool Learning Puzzle Pack", 39900),
        ("Suncity Wooden Abacus with Counting Beads", 24900),
    ]),
]

PRODUCTS = [
    {
        "id": f"prod_{category_index * 50 + product_index + 1:03d}",
        "name": name,
        "category": category,
        "price_minor": price_minor,
        "in_stock": 0 if product_index < 5 else 1,
    }
    for category_index, (category, products) in enumerate(PRODUCT_DATA)
    for product_index, (name, price_minor) in enumerate(products)
]

PRICE_RANGES_MINOR = {
    "Electronics": (50000, 8000000),
    "Clothing": (30000, 500000),
    "Cosmetics": (15000, 300000),
    "Food": (5000, 150000),
    "Shoes": (50000, 800000),
    "Toys": (20000, 500000),
}
assert len(PRODUCTS) == 300
assert {category: sum(p["category"] == category for p in PRODUCTS) for category in CATEGORIES} == {
    category: 50 for category in CATEGORIES
}
assert all(
    PRICE_RANGES_MINOR[product["category"]][0] <= product["price_minor"]
    <= PRICE_RANGES_MINOR[product["category"]][1]
    for product in PRODUCTS
)
assert all(
    sum(p["category"] == category and p["in_stock"] == 0 for p in PRODUCTS) == 5
    for category in CATEGORIES
)

ORDER_STATUSES_WEIGHTED = (
    # status, weight
    ("delivered", 60),
    ("shipped", 15),
    ("confirmed", 10),
    ("pending", 10),
    ("cancelled", 5),
)

SCHEMA_SQL = """
CREATE TABLE product_catalog (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    price_minor INTEGER NOT NULL,
    in_stock    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE orders (
    id            TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL,
    status        TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'INR',
    total_minor   INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT,
    shipping_address_id TEXT REFERENCES addresses(id)
);
CREATE INDEX idx_orders_customer_id ON orders(customer_id);

CREATE TABLE order_items (
    id           TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES orders(id),
    product_id   TEXT NOT NULL REFERENCES product_catalog(id),
    product_name TEXT NOT NULL,
    category     TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    price_minor  INTEGER NOT NULL
);
CREATE INDEX idx_order_items_order_id ON order_items(order_id);

CREATE TABLE shipments (
    id                  TEXT PRIMARY KEY,
    order_id            TEXT NOT NULL REFERENCES orders(id),
    carrier             TEXT,
    tracking_id         TEXT,
    status              TEXT NOT NULL,
    estimated_delivery  TEXT,
    last_updated        TEXT NOT NULL
);
CREATE INDEX idx_shipments_order_id ON shipments(order_id);

CREATE TABLE payments (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES orders(id),
    status          TEXT NOT NULL,
    amount_minor    INTEGER NOT NULL,
    refunded_minor  INTEGER NOT NULL DEFAULT 0,
    payment_method  TEXT,
    paid_at         TEXT
);
CREATE INDEX idx_payments_order_id ON payments(order_id);

CREATE TABLE returns (
    id                TEXT PRIMARY KEY,
    order_id          TEXT NOT NULL REFERENCES orders(id),
    status            TEXT NOT NULL,
    reason            TEXT,
    received_at       TEXT,
    inspection_status TEXT
);
CREATE INDEX idx_returns_order_id ON returns(order_id);

CREATE TABLE tickets (
    id          TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    order_id    TEXT,
    category    TEXT NOT NULL,
    status      TEXT NOT NULL,
    summary     TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_tickets_customer_id ON tickets(customer_id);

CREATE TABLE workflow_runs (
    id           TEXT PRIMARY KEY,
    ticket_id    TEXT NOT NULL REFERENCES tickets(id),
    thread_id    TEXT NOT NULL,
    status       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE action_proposals (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES workflow_runs(id),
    action_type  TEXT NOT NULL,
    payload      TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

CREATE TABLE approvals (
    id          TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES action_proposals(id),
    reviewer_id TEXT NOT NULL,
    decision    TEXT,
    decided_at  TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE audit_events (
    id         TEXT PRIMARY KEY,
    run_id     TEXT,
    actor      TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload    TEXT,
    created_at TEXT NOT NULL
);

DROP TABLE IF EXISTS users;
CREATE TABLE users (
    id          TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    phone       TEXT,
    pin         TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'customer',
    created_at  TEXT NOT NULL,
    CHECK (role IN ('customer', 'reviewer'))
);

DROP TABLE IF EXISTS addresses;
CREATE TABLE addresses (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    label      TEXT NOT NULL DEFAULT 'Home',
    line1      TEXT NOT NULL,
    line2      TEXT,
    city       TEXT NOT NULL,
    state      TEXT NOT NULL,
    pincode    TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 1
);

DROP TABLE IF EXISTS exchanges;
CREATE TABLE exchanges (
    id                    TEXT PRIMARY KEY,
    original_order_id     TEXT NOT NULL REFERENCES orders(id),
    replacement_order_id  TEXT REFERENCES orders(id),
    customer_id           TEXT NOT NULL REFERENCES users(customer_id),
    status                TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    CHECK (status IN ('requested', 'approved', 'rejected', 'completed'))
);

DROP TABLE IF EXISTS exchange_items;
CREATE TABLE exchange_items (
    id                      TEXT PRIMARY KEY,
    exchange_id             TEXT NOT NULL REFERENCES exchanges(id),
    original_order_item_id  TEXT NOT NULL REFERENCES order_items(id),
    replacement_product_id  TEXT REFERENCES product_catalog(id),
    quantity                INTEGER NOT NULL
);

DROP TABLE IF EXISTS return_items;
CREATE TABLE return_items (
    id           TEXT PRIMARY KEY,
    return_id    TEXT NOT NULL REFERENCES returns(id),
    order_item_id TEXT NOT NULL REFERENCES order_items(id),
    quantity     INTEGER NOT NULL,
    reason       TEXT,
    condition    TEXT,
    CHECK (condition IN ('unopened', 'opened', 'damaged'))
);

DROP TABLE IF EXISTS payment_transactions;
CREATE TABLE payment_transactions (
    id                TEXT PRIMARY KEY,
    payment_id        TEXT NOT NULL REFERENCES payments(id),
    type              TEXT NOT NULL,
    amount_minor      INTEGER NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    provider_reference TEXT,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CHECK (type IN ('charge', 'refund', 'partial_refund')),
    CHECK (status IN ('success', 'failed', 'pending'))
);

DROP TABLE IF EXISTS refunds;
CREATE TABLE refunds (
    id              TEXT PRIMARY KEY,
    payment_id      TEXT NOT NULL REFERENCES payments(id),
    order_id        TEXT NOT NULL REFERENCES orders(id),
    proposal_id     TEXT NOT NULL REFERENCES action_proposals(id),
    amount_minor    INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    provider_ref    TEXT,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    CHECK (status IN ('pending', 'success', 'failed'))
);
"""

NEW_TABLES = [
    "audit_events", "approvals", "action_proposals", "workflow_runs",
    "tickets", "returns", "payments", "shipments", "order_items",
    "orders", "product_catalog", "refunds", "payment_transactions",
    "return_items", "exchange_items", "exchanges", "addresses", "users",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    dt = dt.replace(microsecond=0)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def _weighted_choice(rng: random.Random, pairs):
    items, weights = zip(*pairs)
    return rng.choices(items, weights=weights, k=1)[0]


def load_customer_ids(csv_path: Path) -> list[str]:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found at {csv_path}")
    ids: set[str] = set()
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if "customer_id" not in (reader.fieldnames or []):
            raise ValueError("CSV missing required column: customer_id")
        for row in reader:
            cid = (row.get("customer_id") or "").strip()
            if cid:
                ids.add(cid)
    if not ids:
        raise ValueError("No customer IDs found in CSV")
    return sorted(ids)


@dataclass
class SeedCounts:
    customers_sampled: int = 0
    products: int = 0
    orders: int = 0
    order_items: int = 0
    shipments: int = 0
    payments: int = 0
    returns: int = 0


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def recreate_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = OFF;")  # allow drop in any order
    for table in NEW_TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {table};")
    cur.executescript(SCHEMA_SQL)
    cur.execute("PRAGMA foreign_keys = ON;")
    conn.commit()


def insert_catalog(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """Returns list of (product_id, category, price_minor)."""
    cur = conn.cursor()
    rows = []
    for product in PRODUCTS:
        cur.execute(
            "INSERT INTO product_catalog (id, name, category, price_minor, in_stock) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                product["id"],
                product["name"],
                product["category"],
                product["price_minor"],
                product["in_stock"],
            ),
        )
        rows.append((product["id"], product["category"], product["price_minor"]))
    return rows


def seed_users(conn: sqlite3.Connection, rng: random.Random) -> list[dict[str, str]]:
    names = [
        "Aarav Sharma", "Ananya Patel", "Arjun Reddy", "Diya Iyer", "Vivaan Gupta",
        "Saanvi Nair", "Aditya Verma", "Ishita Rao", "Reyansh Mehta", "Aadhya Singh",
        "Krishna Menon", "Myra Joshi", "Ishaan Kapoor", "Kiara Das", "Atharv Kulkarni",
        "Avni Shah", "Kabir Choudhary", "Anika Banerjee", "Rohan Deshmukh", "Pari Malhotra",
        "Devansh Kumar", "Navya Pillai", "Yash Thakur", "Riya Chatterjee", "Dhruv Sinha",
        "Tara Srinivasan", "Karthik Rao", "Meera Krishnan", "Om Prakash", "Ira Bhat",
        "Siddharth Jain", "Pooja Mishra", "Nikhil Shetty", "Kavya Ghosh", "Manav Bansal",
        "Sneha Ranganathan", "Rahul Yadav", "Aishwarya Naidu", "Harsh Vardhan", "Simran Kaur",
        "Varun Malhotra", "Lakshmi Narayanan", "Ankit Saxena", "Madhuri Desai", "Samar Roy",
        "Neha Agarwal", "Pranav Bhatt", "Shreya Mukherjee", "Gaurav Tiwari", "Nandini Bose",
    ]
    address_templates = [
        "12 MG Road", "24 Park Street", "7 Residency Road", "18 Lake View Road",
        "32 Gandhi Nagar", "45 Ashoka Marg", "9 Rose Garden Lane", "16 Station Road",
        "28 Shanti Vihar", "5 Green Park Extension",
    ]
    locations = [
        ("Bangalore", "Karnataka", "560001"),
        ("Mumbai", "Maharashtra", "400001"),
        ("Delhi", "Delhi", "110001"),
        ("Hyderabad", "Telangana", "500001"),
        ("Chennai", "Tamil Nadu", "600001"),
        ("Pune", "Maharashtra", "411001"),
        ("Kolkata", "West Bengal", "700001"),
        ("Ahmedabad", "Gujarat", "380001"),
        ("Jaipur", "Rajasthan", "302001"),
        ("Surat", "Gujarat", "395003"),
    ]

    cur = conn.cursor()
    now = _iso(datetime.now(timezone.utc))
    customers: list[dict[str, str]] = []
    for i, name in enumerate(names):
        user_id = f"usr_{uuid4().hex[:8]}"
        customer_id = f"C{str(i + 1).zfill(6)}"
        email = f"{name.lower().replace(' ', '.')}{i + 1}@example.com"
        cur.execute(
            "INSERT INTO users (id, customer_id, name, email, phone, pin, role, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, customer_id, name, email,
                f"9{rng.randint(100000000, 999999999)}", "1234", "customer", now,
            ),
        )
        customers.append(
            {"id": user_id, "customer_id": customer_id, "name": name, "email": email}
        )

        city, state, pincode = locations[i % len(locations)]
        cur.execute(
            "INSERT INTO addresses (id, user_id, label, line1, city, state, pincode, is_default) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"adr_{uuid4().hex[:8]}", user_id, "Home",
                address_templates[i % len(address_templates)], city, state, pincode, 1,
            ),
        )

    for index, name in enumerate(("Reviewer One", "Reviewer Two"), start=1):
        cur.execute(
            "INSERT INTO users (id, customer_id, name, email, pin, role, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"usr_{uuid4().hex[:8]}", f"R{index:06d}", name,
                f"reviewer.{index}@example.com", "9999", "reviewer", now,
            ),
        )

    return customers


# ---------------------------------------------------------------------------
# Order generation
# ---------------------------------------------------------------------------

def generate_orders(
    conn: sqlite3.Connection,
    rng: random.Random,
    customers: list[dict[str, str]],
    catalog: list[tuple[str, str, int]],
    counts: SeedCounts,
) -> None:
    cur = conn.cursor()
    sampled_customers = rng.sample(customers, k=min(NUM_ORDERS, len(customers)))
    counts.customers_sampled = len(sampled_customers)

    now = datetime.now(timezone.utc)
    product_lookup = {pid: (cat, price) for pid, cat, price in catalog}
    product_ids = list(product_lookup.keys())

    for i in range(1, NUM_ORDERS + 1):
        order_id = f"ord_{i:04d}"
        customer_id = sampled_customers[(i - 1) % len(sampled_customers)]["customer_id"]

        status = _weighted_choice(rng, ORDER_STATUSES_WEIGHTED)
        created_at = now - timedelta(days=rng.randint(1, 120))

        delivered_at = None
        if status == "delivered":
            # 80% of delivered orders: recent (1–10 days), within both windows
            # 20% of delivered orders: aged (35–60 days), outside both windows
            if rng.random() < 0.80:
                delivered_at = now - timedelta(days=rng.randint(1, 10))
            else:
                delivered_at = now - timedelta(days=rng.randint(35, 60))
            

        # 1-3 items per order
        n_items = rng.randint(1, 3)
        chosen_products = rng.sample(product_ids, k=n_items)
        total_minor = 0
        item_rows = []
        for j, pid in enumerate(chosen_products, start=1):
            cat, price = product_lookup[pid]
            qty = rng.randint(1, 2)
            total_minor += price * qty
            item_rows.append((f"{order_id}_item_{j}", order_id, pid, cat, qty, price))

        cur.execute(
            "INSERT INTO orders (id, customer_id, status, currency, total_minor, "
            "created_at, delivered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                order_id, customer_id, status, CURRENCY, total_minor,
                _iso(created_at), _iso(delivered_at) if delivered_at else None,
            ),
        )
        counts.orders += 1

        for item in item_rows:
            item_id, item_order_id, product_id, category, qty, price = item
            cur.execute(
                "INSERT INTO order_items (id, order_id, product_id, product_name, "
                "category, quantity, price_minor) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id, item_order_id, product_id, _product_name(product_id),
                    category, qty, price,
                ),
            )
            counts.order_items += 1

        generate_shipment(cur, order_id, status, created_at, delivered_at, rng, counts)
        generate_payment(cur, order_id, status, total_minor, created_at, rng, counts)
        if status == "delivered" and rng.random() < RETURN_RATE:
            generate_return(cur, order_id, delivered_at, rng, counts)


_PRODUCT_NAME_BY_ID = {product["id"]: product["name"] for product in PRODUCTS}


def _product_name(product_id: str) -> str:
    return _PRODUCT_NAME_BY_ID[product_id]


def generate_shipment(cur, order_id, order_status, created_at, delivered_at, rng, counts):
    shipment_id = f"ship_{order_id}"
    carrier = rng.choice(["BlueDart", "Delhivery", "DTDC", "IndiaPost"])
    tracking_id = f"TRK{rng.randint(10**9, 10**10 - 1)}"

    if order_status == "delivered":
        s_status = "delivered"
        est = delivered_at
    elif order_status == "shipped":
        s_status = rng.choice(["in_transit", "out_for_delivery", "delayed"])
        est = created_at + timedelta(days=rng.randint(3, 9))
    elif order_status == "confirmed":
        s_status = "processing"
        est = created_at + timedelta(days=rng.randint(5, 10))
    elif order_status == "cancelled":
        s_status = "processing"
        est = None
    else:  # pending
        s_status = "processing"
        est = None

    last_updated = delivered_at if delivered_at else created_at
    cur.execute(
        "INSERT INTO shipments (id, order_id, carrier, tracking_id, status, "
        "estimated_delivery, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            shipment_id, order_id, carrier, tracking_id, s_status,
            _iso(est) if est else None, _iso(last_updated),
        ),
    )
    counts.shipments += 1


PAYMENT_METHODS = ["Credit Card", "Debit Card", "Cash", "UPI"]


def generate_payment(cur, order_id, order_status, total_minor, created_at, rng, counts):
    payment_id = f"pay_{order_id}"
    method = rng.choice(PAYMENT_METHODS)

    if order_status == "cancelled":
        p_status = "failed"
        paid_at = None
    elif order_status == "pending":
        p_status = rng.choice(["pending", "paid"])
        paid_at = created_at if p_status == "paid" else None
    else:
        p_status = "paid"
        paid_at = created_at

    cur.execute(
        "INSERT INTO payments (id, order_id, status, amount_minor, refunded_minor, "
        "payment_method, paid_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
        (
            payment_id, order_id, p_status, total_minor, method,
            _iso(paid_at) if paid_at else None,
        ),
    )
    counts.payments += 1


RETURN_REASONS = [
    "Item damaged on arrival",
    "Wrong item received",
    "No longer needed",
    "Does not match description",
    "Found a better price elsewhere",
]


def generate_return(cur, order_id, delivered_at, rng, counts):
    return_id = f"ret_{order_id}"
    received_at = delivered_at + timedelta(days=rng.randint(2, 20))
    inspection_status = _weighted_choice(rng, [("passed", 70), ("failed", 30)])
    status = "passed" if inspection_status == "passed" else "failed"
    # 'status' here mirrors inspection outcome; "received" state is implied
    # by received_at being set. Use 'inspecting' in rare cases to simulate
    # in-progress returns (not yet finally resolved).
    final_status = rng.choices(
        ["passed", "failed", "inspecting", "received"],
        weights=[55, 20, 15, 10],
        k=1,
    )[0]

    cur.execute(
        "INSERT INTO returns (id, order_id, status, reason, received_at, "
        "inspection_status) VALUES (?, ?, ?, ?, ?, ?)",
        (
            return_id, order_id, final_status, rng.choice(RETURN_REASONS),
            _iso(received_at),
            inspection_status if final_status in ("passed", "failed") else None,
        ),
    )
    counts.returns += 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_seed() -> SeedCounts:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    rng = random.Random(SEED)
    counts = SeedCounts()

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        recreate_schema(conn)
        catalog = insert_catalog(conn)
        counts.products = len(catalog)
        customers = seed_users(conn, rng)
        generate_orders(conn, rng, customers, catalog, counts)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return counts


def main() -> None:
    try:
        counts = run_seed()
    except Exception as exc:  # noqa: BLE001
        print(f"[SEED FAILED] {exc}", file=sys.stderr)
        sys.exit(1)

    print("[SEED OK]")
    print(f"  DB path:            {DB_PATH}")
    print(f"  Customers sampled:  {counts.customers_sampled}")
    print(f"  Products:           {counts.products}")
    print(f"  Orders:             {counts.orders}")
    print(f"  Order items:        {counts.order_items}")
    print(f"  Shipments:          {counts.shipments}")
    print(f"  Payments:           {counts.payments}")
    print(f"  Returns:            {counts.returns}")
    print("Run verify_seed.py to inspect sample rows.")


if __name__ == "__main__":
    main()