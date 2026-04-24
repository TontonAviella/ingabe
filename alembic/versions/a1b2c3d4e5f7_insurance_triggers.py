"""insurance_triggers table with seed data

Revision ID: a1b2c3d4e5f7
Revises: 47463555a0f8
Create Date: 2026-04-24
"""
from alembic import op
import sqlalchemy as sa

revision: str = "a1b2c3d4e5f7"
down_revision: str = "47463555a0f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "insurance_triggers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("crop", sa.Text(), nullable=False),
        sa.Column("season", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("signal", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("district", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("source", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("season IN ('A', 'B')", name="ck_insurance_triggers_season"),
        sa.CheckConstraint(
            "phase IN ('planting', 'vegetative', 'flowering', 'grain_fill', 'maturity', 'full_season')",
            name="ck_insurance_triggers_phase",
        ),
        sa.CheckConstraint(
            "signal IN ('rainfall_cumulative', 'spi', 'ndvi_z_score', "
            "'dry_spell_days', 'et_anomaly', 'soil_moisture')",
            name="ck_insurance_triggers_signal",
        ),
        sa.CheckConstraint(
            "direction IN ('below', 'above')",
            name="ck_insurance_triggers_direction",
        ),
    )

    op.execute("""
        CREATE OR REPLACE FUNCTION update_insurance_triggers_timestamp()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_insurance_triggers_updated_at
        BEFORE UPDATE ON insurance_triggers
        FOR EACH ROW
        EXECUTE FUNCTION update_insurance_triggers_timestamp();
    """)

    op.create_index(
        "uq_insurance_triggers_key",
        "insurance_triggers",
        [sa.text("crop, season, phase, signal, COALESCE(district, '')")],
        unique=True,
    )

    # Seed data: maize, beans, rice × Seasons A & B
    # Thresholds based on NAIS product parameters + standard parametric practice
    op.execute("""
        INSERT INTO insurance_triggers (crop, season, phase, signal, direction, threshold, weight, description, source) VALUES
        -- Maize Season A
        ('maize', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'NAIS parametric'),
        ('maize', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('maize', 'A', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'NAIS parametric'),
        ('maize', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2'),
        ('maize', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        ('maize', 'A', 'flowering', 'rainfall_cumulative', 'below', 40.0, 1.0, 'Flowering phase rainfall below 40mm critical minimum', 'Agronomic'),
        ('maize', 'A', 'flowering', 'dry_spell_days', 'above', 10.0, 0.9, 'Dry spell during flowering exceeds 10 days', 'Agronomic'),
        -- Maize Season B
        ('maize', 'B', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'NAIS parametric'),
        ('maize', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('maize', 'B', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'NAIS parametric'),
        ('maize', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2'),
        ('maize', 'B', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        ('maize', 'B', 'flowering', 'rainfall_cumulative', 'below', 40.0, 1.0, 'Flowering phase rainfall below 40mm critical minimum', 'Agronomic'),
        ('maize', 'B', 'flowering', 'dry_spell_days', 'above', 10.0, 0.9, 'Dry spell during flowering exceeds 10 days', 'Agronomic'),
        -- Beans Season A
        ('beans', 'A', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'NAIS parametric'),
        ('beans', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('beans', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'NAIS parametric'),
        ('beans', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2'),
        ('beans', 'A', 'flowering', 'rainfall_cumulative', 'below', 30.0, 1.0, 'Flowering phase rainfall below 30mm', 'Agronomic'),
        -- Beans Season B
        ('beans', 'B', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'NAIS parametric'),
        ('beans', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('beans', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'NAIS parametric'),
        ('beans', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2'),
        ('beans', 'B', 'flowering', 'rainfall_cumulative', 'below', 30.0, 1.0, 'Flowering phase rainfall below 30mm', 'Agronomic'),
        -- Rice Season A
        ('rice', 'A', 'full_season', 'rainfall_cumulative', 'below', 150.0, 1.0, 'Season cumulative rainfall below 150mm', 'NAIS parametric'),
        ('rice', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('rice', 'A', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Rice sensitivity'),
        ('rice', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2'),
        ('rice', 'A', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        -- Rice Season B
        ('rice', 'B', 'full_season', 'rainfall_cumulative', 'below', 150.0, 1.0, 'Season cumulative rainfall below 150mm', 'NAIS parametric'),
        ('rice', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('rice', 'B', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Rice sensitivity'),
        ('rice', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2'),
        ('rice', 'B', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3');
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_insurance_triggers_updated_at ON insurance_triggers")
    op.execute("DROP FUNCTION IF EXISTS update_insurance_triggers_timestamp()")
    op.drop_table("insurance_triggers")
