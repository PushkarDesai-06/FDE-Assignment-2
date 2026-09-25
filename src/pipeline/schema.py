"""The source schema contract, and how the two fleets are reconciled.

Yellow and green taxi extracts describe the same real-world event - a taxi
trip - but do not share a schema:

  * pickup/dropoff timestamps are prefixed `tpep_` (yellow) vs `lpep_` (green)
  * green carries `ehail_fee` and `trip_type`, which yellow does not
  * yellow carries an airport fee column, which green does not
  * the airport fee column is spelled `Airport_fee` in some months and
    `airport_fee` in others, so it is matched case-insensitively

Keeping this contract in one module means a schema drift in a future month
fails loudly in the completeness check instead of silently producing a
column of NULLs in a metric.
"""

from __future__ import annotations

# Columns every fleet must provide for the pipeline to work at all.
REQUIRED_COMMON = [
    "VendorID",
    "passenger_count",
    "trip_distance",
    "RatecodeID",
    "store_and_fwd_flag",
    "PULocationID",
    "DOLocationID",
    "payment_type",
    "fare_amount",
    "extra",
    "mta_tax",
    "tip_amount",
    "tolls_amount",
    "improvement_surcharge",
    "total_amount",
    "congestion_surcharge",
]

# Fleet-specific timestamp column names.
TIMESTAMP_COLUMNS = {
    "yellow": ("tpep_pickup_datetime", "tpep_dropoff_datetime"),
    "green": ("lpep_pickup_datetime", "lpep_dropoff_datetime"),
}

# Optional columns: present in some fleets/months, absent in others.
# Absence is expected and is filled with NULL, never treated as an error.
OPTIONAL_COLUMNS = {
    "yellow": ["Airport_fee", "airport_fee"],
    "green": ["ehail_fee", "trip_type"],
}


def required_columns(fleet: str) -> list[str]:
    """Columns that must exist in a raw extract for this fleet."""
    if fleet not in TIMESTAMP_COLUMNS:
        raise ValueError(f"Unknown fleet: {fleet}")
    return [*TIMESTAMP_COLUMNS[fleet], *REQUIRED_COMMON]


def check_schema(fleet: str, actual_columns: list[str]) -> list[str]:
    """Return the list of required columns missing from a raw extract.

    An empty list means the extract satisfies the contract. Extra columns
    are allowed (TLC adds columns over time); missing ones are not.
    """
    actual_lower = {c.lower() for c in actual_columns}
    return [c for c in required_columns(fleet) if c.lower() not in actual_lower]


def airport_fee_expr(fleet: str, actual_columns: list[str]) -> str:
    """SQL expression for the airport fee, whatever it is called this month.

    Green has no airport fee column at all, so it resolves to NULL.
    """
    by_lower = {c.lower(): c for c in actual_columns}
    for candidate in ("airport_fee",):
        if candidate in by_lower:
            return f'"{by_lower[candidate]}"'
    return "CAST(NULL AS DOUBLE)"


def normalise_select(fleet: str, actual_columns: list[str]) -> str:
    """Build the SELECT that maps a fleet's raw extract onto one common shape.

    This is the single point where yellow and green become comparable.
    """
    pickup, dropoff = TIMESTAMP_COLUMNS[fleet]
    by_lower = {c.lower(): c for c in actual_columns}

    def col(name: str, cast: str = "DOUBLE") -> str:
        """Reference a column if present, else a typed NULL."""
        if name.lower() in by_lower:
            return f'"{by_lower[name.lower()]}"'
        return f"CAST(NULL AS {cast})"

    return f"""
        SELECT
            '{fleet}'                              AS fleet,
            {col("VendorID", "INTEGER")}           AS vendor_id,
            {col(pickup, "TIMESTAMP")}             AS pickup_ts,
            {col(dropoff, "TIMESTAMP")}            AS dropoff_ts,
            {col("passenger_count", "BIGINT")}     AS passenger_count,
            {col("trip_distance")}                 AS trip_distance_mi,
            {col("RatecodeID", "BIGINT")}          AS ratecode_id,
            {col("store_and_fwd_flag", "VARCHAR")} AS store_and_fwd_flag,
            {col("PULocationID", "INTEGER")}       AS pu_location_id,
            {col("DOLocationID", "INTEGER")}       AS do_location_id,
            {col("payment_type", "BIGINT")}        AS payment_type,
            {col("fare_amount")}                   AS fare_amount,
            {col("extra")}                         AS extra,
            {col("mta_tax")}                       AS mta_tax,
            {col("tip_amount")}                    AS tip_amount,
            {col("tolls_amount")}                  AS tolls_amount,
            {col("improvement_surcharge")}         AS improvement_surcharge,
            {col("total_amount")}                  AS total_amount,
            {col("congestion_surcharge")}          AS congestion_surcharge,
            {airport_fee_expr(fleet, actual_columns)} AS airport_fee,
            {col("ehail_fee")}                     AS ehail_fee,
            {col("trip_type", "BIGINT")}           AS trip_type
    """
