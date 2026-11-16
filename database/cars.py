"""Schema and fictional starter inventory for the cars table."""

# Fields are ordered to match the INSERT statement in create_cars.
SAMPLE_CARS = [
    ("AutoCare", "Urban Lite", 2026, "Hatchback", "Petrol", "5-speed manual", 5, 82, 285, 749000),
    ("AutoCare", "Trailview", 2026, "Compact SUV", "Petrol hybrid", "CVT", 5, 114, 440, 1399000),
    ("AutoCare", "Horizon EV", 2026, "Electric crossover", "Electric", "Single-speed", 5, 201, 510, 2199000),
    ("AutoCare", "Metro Zip", 2026, "Hatchback", "Petrol", "5-speed manual", 5, 75, 260, 659000),
    ("AutoCare", "Cityline", 2026, "Sedan", "Petrol", "CVT", 5, 119, 480, 1099000),
    ("AutoCare", "Ridgeline", 2026, "SUV", "Diesel", "6-speed automatic", 7, 168, 375, 1949000),
    ("AutoCare", "Meadow", 2026, "MPV", "Petrol hybrid", "CVT", 7, 138, 300, 1649000),
    ("AutoCare", "Skyline EV", 2026, "Sedan", "Electric", "Single-speed", 5, 228, 460, 2699000),
    ("AutoCare", "Harbor", 2026, "Wagon", "Petrol", "6-speed automatic", 5, 148, 565, 1549000),
    ("AutoCare", "Summit", 2026, "SUV", "Petrol hybrid", "8-speed automatic", 7, 236, 420, 2999000),
]


def create_cars(db):
    db.execute("""CREATE TABLE cars (
        car_id INTEGER PRIMARY KEY,
        make TEXT NOT NULL,
        model TEXT NOT NULL,
        model_year INTEGER NOT NULL,
        body_style TEXT NOT NULL,
        fuel_type TEXT NOT NULL,
        transmission TEXT NOT NULL,
        seats INTEGER NOT NULL CHECK (seats > 0),
        power_hp INTEGER NOT NULL CHECK (power_hp > 0),
        boot_litres INTEGER NOT NULL CHECK (boot_litres >= 0),
        price_inr INTEGER NOT NULL CHECK (price_inr > 0),
        UNIQUE (make, model, model_year)
    )""")
    db.executemany(
        """INSERT INTO cars (make, model, model_year, body_style, fuel_type,
           transmission, seats, power_hp, boot_litres, price_inr)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        SAMPLE_CARS,
    )


def list_cars(db):
    return [dict(row) for row in db.execute("SELECT * FROM cars ORDER BY make, model, model_year")]


def add_car(db, car):
    db.execute(
        """INSERT INTO cars (make, model, model_year, body_style, fuel_type,
           transmission, seats, power_hp, boot_litres, price_inr)
           VALUES (:make, :model, :model_year, :body_style, :fuel_type,
                   :transmission, :seats, :power_hp, :boot_litres, :price_inr)""",
        car,
    )


def update_car(db, car_id, car):
    return db.execute(
        """UPDATE cars SET make = :make, model = :model, model_year = :model_year,
           body_style = :body_style, fuel_type = :fuel_type, transmission = :transmission,
           seats = :seats, power_hp = :power_hp, boot_litres = :boot_litres, price_inr = :price_inr
           WHERE car_id = :car_id""",
        {**car, "car_id": car_id},
    ).rowcount == 1


def remove_car(db, car_id):
    return db.execute("DELETE FROM cars WHERE car_id = ?", (car_id,)).rowcount == 1


def has_model(db, model):
    return db.execute("SELECT 1 FROM cars WHERE model = ? LIMIT 1", (model,)).fetchone() is not None
