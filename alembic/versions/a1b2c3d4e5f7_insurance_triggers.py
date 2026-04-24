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
            "'dry_spell_days', 'et_anomaly', 'soil_moisture', 'sar_backscatter')",
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

    # Seed data: all insurable crops × available seasons
    # Thresholds based on NAIS product parameters + standard parametric practice
    # Each crop gets 5 full_season triggers per season (rainfall, SPI, dry spell, NDVI, ET/soil)
    # Thresholds are crop-appropriate: water-loving crops (rice) need more rain, drought-tolerant (sorghum, cassava) need less
    op.execute("""
        INSERT INTO insurance_triggers (crop, season, phase, signal, direction, threshold, weight, description, source) VALUES
        -- =================================================================
        -- CEREALS
        -- =================================================================
        -- Maize Season A
        ('maize', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'NAIS parametric'),
        ('maize', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('maize', 'A', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'NAIS parametric'),
        ('maize', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('maize', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        ('maize', 'A', 'flowering', 'rainfall_cumulative', 'below', 40.0, 1.0, 'Flowering phase rainfall below 40mm critical minimum', 'Agronomic'),
        ('maize', 'A', 'flowering', 'dry_spell_days', 'above', 10.0, 0.9, 'Dry spell during flowering exceeds 10 days', 'Agronomic'),
        -- Maize Season B
        ('maize', 'B', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'NAIS parametric'),
        ('maize', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('maize', 'B', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'NAIS parametric'),
        ('maize', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('maize', 'B', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        ('maize', 'B', 'flowering', 'rainfall_cumulative', 'below', 40.0, 1.0, 'Flowering phase rainfall below 40mm critical minimum', 'Agronomic'),
        ('maize', 'B', 'flowering', 'dry_spell_days', 'above', 10.0, 0.9, 'Dry spell during flowering exceeds 10 days', 'Agronomic'),
        -- Beans Season A
        ('beans', 'A', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'NAIS parametric'),
        ('beans', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('beans', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'NAIS parametric'),
        ('beans', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('beans', 'A', 'flowering', 'rainfall_cumulative', 'below', 30.0, 1.0, 'Flowering phase rainfall below 30mm', 'Agronomic'),
        -- Beans Season B
        ('beans', 'B', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'NAIS parametric'),
        ('beans', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('beans', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'NAIS parametric'),
        ('beans', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('beans', 'B', 'flowering', 'rainfall_cumulative', 'below', 30.0, 1.0, 'Flowering phase rainfall below 30mm', 'Agronomic'),
        -- Rice Season A
        ('rice', 'A', 'full_season', 'rainfall_cumulative', 'below', 150.0, 1.0, 'Season cumulative rainfall below 150mm', 'NAIS parametric'),
        ('rice', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('rice', 'A', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Rice sensitivity'),
        ('rice', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('rice', 'A', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        -- Rice Season B
        ('rice', 'B', 'full_season', 'rainfall_cumulative', 'below', 150.0, 1.0, 'Season cumulative rainfall below 150mm', 'NAIS parametric'),
        ('rice', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('rice', 'B', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Rice sensitivity'),
        ('rice', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('rice', 'B', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        -- Sorghum Season A (drought-tolerant)
        ('sorghum', 'A', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('sorghum', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('sorghum', 'A', 'full_season', 'dry_spell_days', 'above', 20.0, 0.6, 'Maximum dry spell exceeds 20 days', 'Parametric'),
        ('sorghum', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('sorghum', 'A', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        -- Sorghum Season B
        ('sorghum', 'B', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('sorghum', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('sorghum', 'B', 'full_season', 'dry_spell_days', 'above', 20.0, 0.6, 'Maximum dry spell exceeds 20 days', 'Parametric'),
        ('sorghum', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('sorghum', 'B', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        -- Wheat Season A (marshlands/highlands)
        ('wheat', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('wheat', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('wheat', 'A', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('wheat', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('wheat', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        -- Finger millet Season A & B
        ('finger_millet', 'A', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('finger_millet', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('finger_millet', 'A', 'full_season', 'dry_spell_days', 'above', 18.0, 0.6, 'Maximum dry spell exceeds 18 days', 'Parametric'),
        ('finger_millet', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('finger_millet', 'A', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        ('finger_millet', 'B', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('finger_millet', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('finger_millet', 'B', 'full_season', 'dry_spell_days', 'above', 18.0, 0.6, 'Maximum dry spell exceeds 18 days', 'Parametric'),
        ('finger_millet', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('finger_millet', 'B', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        -- =================================================================
        -- TUBERS & ROOTS
        -- =================================================================
        -- Potato Season A & B
        ('potato', 'A', 'full_season', 'rainfall_cumulative', 'below', 120.0, 1.0, 'Season cumulative rainfall below 120mm', 'Parametric'),
        ('potato', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('potato', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('potato', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('potato', 'A', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        ('potato', 'B', 'full_season', 'rainfall_cumulative', 'below', 120.0, 1.0, 'Season cumulative rainfall below 120mm', 'Parametric'),
        ('potato', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('potato', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('potato', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('potato', 'B', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        -- Sweet potato Season A & B
        ('sweet_potato', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('sweet_potato', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('sweet_potato', 'A', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('sweet_potato', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('sweet_potato', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        ('sweet_potato', 'B', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('sweet_potato', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('sweet_potato', 'B', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('sweet_potato', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('sweet_potato', 'B', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        -- Cassava Season A & B (drought-tolerant, long cycle)
        ('cassava', 'A', 'full_season', 'rainfall_cumulative', 'below', 60.0, 1.0, 'Season cumulative rainfall below 60mm', 'Parametric'),
        ('cassava', 'A', 'full_season', 'spi', 'below', -1.5, 0.8, 'SPI indicates severe drought', 'WMO standard'),
        ('cassava', 'A', 'full_season', 'dry_spell_days', 'above', 25.0, 0.5, 'Maximum dry spell exceeds 25 days', 'Parametric'),
        ('cassava', 'A', 'full_season', 'ndvi_z_score', 'below', -2.0, 0.7, 'NDVI anomaly indicates extreme vegetation stress', 'Sentinel-2/SAR'),
        ('cassava', 'A', 'full_season', 'et_anomaly', 'below', -30.0, 0.4, 'ET anomaly exceeds -30% deficit', 'WaPOR v3'),
        ('cassava', 'B', 'full_season', 'rainfall_cumulative', 'below', 60.0, 1.0, 'Season cumulative rainfall below 60mm', 'Parametric'),
        ('cassava', 'B', 'full_season', 'spi', 'below', -1.5, 0.8, 'SPI indicates severe drought', 'WMO standard'),
        ('cassava', 'B', 'full_season', 'dry_spell_days', 'above', 25.0, 0.5, 'Maximum dry spell exceeds 25 days', 'Parametric'),
        ('cassava', 'B', 'full_season', 'ndvi_z_score', 'below', -2.0, 0.7, 'NDVI anomaly indicates extreme vegetation stress', 'Sentinel-2/SAR'),
        ('cassava', 'B', 'full_season', 'et_anomaly', 'below', -30.0, 0.4, 'ET anomaly exceeds -30% deficit', 'WaPOR v3'),
        -- Yam Season A & B
        ('yam', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('yam', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('yam', 'A', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('yam', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('yam', 'A', 'full_season', 'soil_moisture', 'below', 20.0, 0.5, 'Soil moisture below 20%', 'WaPOR v3'),
        ('yam', 'B', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('yam', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('yam', 'B', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('yam', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('yam', 'B', 'full_season', 'soil_moisture', 'below', 20.0, 0.5, 'Soil moisture below 20%', 'WaPOR v3'),
        -- Taro Season A & B (moisture-loving)
        ('taro', 'A', 'full_season', 'rainfall_cumulative', 'below', 140.0, 1.0, 'Season cumulative rainfall below 140mm', 'Parametric'),
        ('taro', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('taro', 'A', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('taro', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('taro', 'A', 'full_season', 'soil_moisture', 'below', 30.0, 0.6, 'Soil moisture below 30%', 'WaPOR v3'),
        ('taro', 'B', 'full_season', 'rainfall_cumulative', 'below', 140.0, 1.0, 'Season cumulative rainfall below 140mm', 'Parametric'),
        ('taro', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('taro', 'B', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('taro', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('taro', 'B', 'full_season', 'soil_moisture', 'below', 30.0, 0.6, 'Soil moisture below 30%', 'WaPOR v3'),
        -- =================================================================
        -- LEGUMES
        -- =================================================================
        -- Soybean Season A & B
        ('soybean', 'A', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('soybean', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('soybean', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('soybean', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('soybean', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        ('soybean', 'B', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('soybean', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('soybean', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('soybean', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('soybean', 'B', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        -- Groundnut Season A & B
        ('groundnut', 'A', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('groundnut', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('groundnut', 'A', 'full_season', 'dry_spell_days', 'above', 14.0, 0.6, 'Maximum dry spell exceeds 14 days', 'Parametric'),
        ('groundnut', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('groundnut', 'A', 'full_season', 'soil_moisture', 'below', 20.0, 0.5, 'Soil moisture below 20%', 'WaPOR v3'),
        ('groundnut', 'B', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('groundnut', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('groundnut', 'B', 'full_season', 'dry_spell_days', 'above', 14.0, 0.6, 'Maximum dry spell exceeds 14 days', 'Parametric'),
        ('groundnut', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('groundnut', 'B', 'full_season', 'soil_moisture', 'below', 20.0, 0.5, 'Soil moisture below 20%', 'WaPOR v3'),
        -- Peas Season A & B
        ('peas', 'A', 'full_season', 'rainfall_cumulative', 'below', 70.0, 1.0, 'Season cumulative rainfall below 70mm', 'Parametric'),
        ('peas', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('peas', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('peas', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('peas', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        ('peas', 'B', 'full_season', 'rainfall_cumulative', 'below', 70.0, 1.0, 'Season cumulative rainfall below 70mm', 'Parametric'),
        ('peas', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('peas', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('peas', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('peas', 'B', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        -- Cowpea Season A & B (drought-tolerant)
        ('cowpea', 'A', 'full_season', 'rainfall_cumulative', 'below', 60.0, 1.0, 'Season cumulative rainfall below 60mm', 'Parametric'),
        ('cowpea', 'A', 'full_season', 'spi', 'below', -1.5, 0.8, 'SPI indicates severe drought', 'WMO standard'),
        ('cowpea', 'A', 'full_season', 'dry_spell_days', 'above', 20.0, 0.5, 'Maximum dry spell exceeds 20 days', 'Parametric'),
        ('cowpea', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('cowpea', 'A', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        ('cowpea', 'B', 'full_season', 'rainfall_cumulative', 'below', 60.0, 1.0, 'Season cumulative rainfall below 60mm', 'Parametric'),
        ('cowpea', 'B', 'full_season', 'spi', 'below', -1.5, 0.8, 'SPI indicates severe drought', 'WMO standard'),
        ('cowpea', 'B', 'full_season', 'dry_spell_days', 'above', 20.0, 0.5, 'Maximum dry spell exceeds 20 days', 'Parametric'),
        ('cowpea', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('cowpea', 'B', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        -- Pigeon pea Season A & B
        ('pigeon_pea', 'A', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('pigeon_pea', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('pigeon_pea', 'A', 'full_season', 'dry_spell_days', 'above', 18.0, 0.6, 'Maximum dry spell exceeds 18 days', 'Parametric'),
        ('pigeon_pea', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('pigeon_pea', 'A', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        ('pigeon_pea', 'B', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('pigeon_pea', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('pigeon_pea', 'B', 'full_season', 'dry_spell_days', 'above', 18.0, 0.6, 'Maximum dry spell exceeds 18 days', 'Parametric'),
        ('pigeon_pea', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('pigeon_pea', 'B', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        -- =================================================================
        -- VEGETABLES
        -- =================================================================
        -- Tomato Season A & B (water-sensitive)
        ('tomato', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('tomato', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('tomato', 'A', 'full_season', 'dry_spell_days', 'above', 10.0, 0.8, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('tomato', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('tomato', 'A', 'full_season', 'et_anomaly', 'below', -15.0, 0.5, 'ET anomaly exceeds -15% deficit', 'WaPOR v3'),
        ('tomato', 'B', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('tomato', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('tomato', 'B', 'full_season', 'dry_spell_days', 'above', 10.0, 0.8, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('tomato', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('tomato', 'B', 'full_season', 'et_anomaly', 'below', -15.0, 0.5, 'ET anomaly exceeds -15% deficit', 'WaPOR v3'),
        -- Onion Season A & B
        ('onion', 'A', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('onion', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('onion', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('onion', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('onion', 'A', 'full_season', 'soil_moisture', 'below', 20.0, 0.5, 'Soil moisture below 20%', 'WaPOR v3'),
        ('onion', 'B', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('onion', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('onion', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('onion', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('onion', 'B', 'full_season', 'soil_moisture', 'below', 20.0, 0.5, 'Soil moisture below 20%', 'WaPOR v3'),
        -- Cabbage Season A & B
        ('cabbage', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('cabbage', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('cabbage', 'A', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('cabbage', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('cabbage', 'A', 'full_season', 'et_anomaly', 'below', -15.0, 0.5, 'ET anomaly exceeds -15% deficit', 'WaPOR v3'),
        ('cabbage', 'B', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('cabbage', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('cabbage', 'B', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('cabbage', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('cabbage', 'B', 'full_season', 'et_anomaly', 'below', -15.0, 0.5, 'ET anomaly exceeds -15% deficit', 'WaPOR v3'),
        -- Carrot Season A & B
        ('carrot', 'A', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('carrot', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('carrot', 'A', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('carrot', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('carrot', 'A', 'full_season', 'soil_moisture', 'below', 22.0, 0.5, 'Soil moisture below 22%', 'WaPOR v3'),
        ('carrot', 'B', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('carrot', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('carrot', 'B', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('carrot', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('carrot', 'B', 'full_season', 'soil_moisture', 'below', 22.0, 0.5, 'Soil moisture below 22%', 'WaPOR v3'),
        -- Chili Season A & B
        ('chili', 'A', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('chili', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('chili', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('chili', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('chili', 'A', 'full_season', 'et_anomaly', 'below', -18.0, 0.4, 'ET anomaly exceeds -18% deficit', 'WaPOR v3'),
        ('chili', 'B', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('chili', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('chili', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('chili', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('chili', 'B', 'full_season', 'et_anomaly', 'below', -18.0, 0.4, 'ET anomaly exceeds -18% deficit', 'WaPOR v3'),
        -- Eggplant Season A & B
        ('eggplant', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('eggplant', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('eggplant', 'A', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('eggplant', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('eggplant', 'A', 'full_season', 'et_anomaly', 'below', -15.0, 0.5, 'ET anomaly exceeds -15% deficit', 'WaPOR v3'),
        ('eggplant', 'B', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('eggplant', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('eggplant', 'B', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('eggplant', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('eggplant', 'B', 'full_season', 'et_anomaly', 'below', -15.0, 0.5, 'ET anomaly exceeds -15% deficit', 'WaPOR v3'),
        -- Green pepper Season A & B
        ('green_pepper', 'A', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('green_pepper', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('green_pepper', 'A', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('green_pepper', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('green_pepper', 'A', 'full_season', 'et_anomaly', 'below', -15.0, 0.5, 'ET anomaly exceeds -15% deficit', 'WaPOR v3'),
        ('green_pepper', 'B', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('green_pepper', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('green_pepper', 'B', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('green_pepper', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('green_pepper', 'B', 'full_season', 'et_anomaly', 'below', -15.0, 0.5, 'ET anomaly exceeds -15% deficit', 'WaPOR v3'),
        -- Garlic Season A & B
        ('garlic', 'A', 'full_season', 'rainfall_cumulative', 'below', 70.0, 1.0, 'Season cumulative rainfall below 70mm', 'Parametric'),
        ('garlic', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('garlic', 'A', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('garlic', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('garlic', 'A', 'full_season', 'soil_moisture', 'below', 18.0, 0.5, 'Soil moisture below 18%', 'WaPOR v3'),
        ('garlic', 'B', 'full_season', 'rainfall_cumulative', 'below', 70.0, 1.0, 'Season cumulative rainfall below 70mm', 'Parametric'),
        ('garlic', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('garlic', 'B', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('garlic', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('garlic', 'B', 'full_season', 'soil_moisture', 'below', 18.0, 0.5, 'Soil moisture below 18%', 'WaPOR v3'),
        -- Amaranth Season A & B (fast-growing, moderate needs)
        ('amaranth', 'A', 'full_season', 'rainfall_cumulative', 'below', 60.0, 1.0, 'Season cumulative rainfall below 60mm', 'Parametric'),
        ('amaranth', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('amaranth', 'A', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('amaranth', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('amaranth', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        ('amaranth', 'B', 'full_season', 'rainfall_cumulative', 'below', 60.0, 1.0, 'Season cumulative rainfall below 60mm', 'Parametric'),
        ('amaranth', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('amaranth', 'B', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('amaranth', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('amaranth', 'B', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        -- =================================================================
        -- FRUITS
        -- =================================================================
        -- Banana Season A & B (high water needs)
        ('banana', 'A', 'full_season', 'rainfall_cumulative', 'below', 150.0, 1.0, 'Season cumulative rainfall below 150mm', 'Parametric'),
        ('banana', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('banana', 'A', 'full_season', 'dry_spell_days', 'above', 10.0, 0.8, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('banana', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('banana', 'A', 'full_season', 'soil_moisture', 'below', 30.0, 0.6, 'Soil moisture below 30%', 'WaPOR v3'),
        ('banana', 'B', 'full_season', 'rainfall_cumulative', 'below', 150.0, 1.0, 'Season cumulative rainfall below 150mm', 'Parametric'),
        ('banana', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('banana', 'B', 'full_season', 'dry_spell_days', 'above', 10.0, 0.8, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('banana', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('banana', 'B', 'full_season', 'soil_moisture', 'below', 30.0, 0.6, 'Soil moisture below 30%', 'WaPOR v3'),
        -- Avocado Season A (perennial)
        ('avocado', 'A', 'full_season', 'rainfall_cumulative', 'below', 120.0, 1.0, 'Season cumulative rainfall below 120mm', 'Parametric'),
        ('avocado', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('avocado', 'A', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('avocado', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('avocado', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        -- Mango Season A (perennial)
        ('mango', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('mango', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('mango', 'A', 'full_season', 'dry_spell_days', 'above', 20.0, 0.5, 'Maximum dry spell exceeds 20 days', 'Parametric'),
        ('mango', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('mango', 'A', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        -- Passion fruit Season A & B
        ('passion_fruit', 'A', 'full_season', 'rainfall_cumulative', 'below', 120.0, 1.0, 'Season cumulative rainfall below 120mm', 'Parametric'),
        ('passion_fruit', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('passion_fruit', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('passion_fruit', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('passion_fruit', 'A', 'full_season', 'et_anomaly', 'below', -18.0, 0.4, 'ET anomaly exceeds -18% deficit', 'WaPOR v3'),
        ('passion_fruit', 'B', 'full_season', 'rainfall_cumulative', 'below', 120.0, 1.0, 'Season cumulative rainfall below 120mm', 'Parametric'),
        ('passion_fruit', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('passion_fruit', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('passion_fruit', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('passion_fruit', 'B', 'full_season', 'et_anomaly', 'below', -18.0, 0.4, 'ET anomaly exceeds -18% deficit', 'WaPOR v3'),
        -- Pineapple Season A (drought-tolerant perennial)
        ('pineapple', 'A', 'full_season', 'rainfall_cumulative', 'below', 60.0, 1.0, 'Season cumulative rainfall below 60mm', 'Parametric'),
        ('pineapple', 'A', 'full_season', 'spi', 'below', -1.5, 0.8, 'SPI indicates severe drought', 'WMO standard'),
        ('pineapple', 'A', 'full_season', 'dry_spell_days', 'above', 25.0, 0.5, 'Maximum dry spell exceeds 25 days', 'Parametric'),
        ('pineapple', 'A', 'full_season', 'ndvi_z_score', 'below', -2.0, 0.7, 'NDVI anomaly indicates extreme vegetation stress', 'Sentinel-2/SAR'),
        ('pineapple', 'A', 'full_season', 'et_anomaly', 'below', -30.0, 0.4, 'ET anomaly exceeds -30% deficit', 'WaPOR v3'),
        -- Papaya Season A & B
        ('papaya', 'A', 'full_season', 'rainfall_cumulative', 'below', 120.0, 1.0, 'Season cumulative rainfall below 120mm', 'Parametric'),
        ('papaya', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('papaya', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('papaya', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('papaya', 'A', 'full_season', 'et_anomaly', 'below', -18.0, 0.4, 'ET anomaly exceeds -18% deficit', 'WaPOR v3'),
        ('papaya', 'B', 'full_season', 'rainfall_cumulative', 'below', 120.0, 1.0, 'Season cumulative rainfall below 120mm', 'Parametric'),
        ('papaya', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('papaya', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('papaya', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('papaya', 'B', 'full_season', 'et_anomaly', 'below', -18.0, 0.4, 'ET anomaly exceeds -18% deficit', 'WaPOR v3'),
        -- Citrus Season A (perennial)
        ('citrus', 'A', 'full_season', 'rainfall_cumulative', 'below', 110.0, 1.0, 'Season cumulative rainfall below 110mm', 'Parametric'),
        ('citrus', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('citrus', 'A', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('citrus', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('citrus', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        -- Strawberry Season A & B
        ('strawberry', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('strawberry', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('strawberry', 'A', 'full_season', 'dry_spell_days', 'above', 8.0, 0.8, 'Maximum dry spell exceeds 8 days', 'Parametric'),
        ('strawberry', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('strawberry', 'A', 'full_season', 'soil_moisture', 'below', 28.0, 0.6, 'Soil moisture below 28%', 'WaPOR v3'),
        ('strawberry', 'B', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('strawberry', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('strawberry', 'B', 'full_season', 'dry_spell_days', 'above', 8.0, 0.8, 'Maximum dry spell exceeds 8 days', 'Parametric'),
        ('strawberry', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('strawberry', 'B', 'full_season', 'soil_moisture', 'below', 28.0, 0.6, 'Soil moisture below 28%', 'WaPOR v3'),
        -- Tree tomato Season A & B
        ('tree_tomato', 'A', 'full_season', 'rainfall_cumulative', 'below', 120.0, 1.0, 'Season cumulative rainfall below 120mm', 'Parametric'),
        ('tree_tomato', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('tree_tomato', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('tree_tomato', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('tree_tomato', 'A', 'full_season', 'et_anomaly', 'below', -18.0, 0.4, 'ET anomaly exceeds -18% deficit', 'WaPOR v3'),
        ('tree_tomato', 'B', 'full_season', 'rainfall_cumulative', 'below', 120.0, 1.0, 'Season cumulative rainfall below 120mm', 'Parametric'),
        ('tree_tomato', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('tree_tomato', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('tree_tomato', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('tree_tomato', 'B', 'full_season', 'et_anomaly', 'below', -18.0, 0.4, 'ET anomaly exceeds -18% deficit', 'WaPOR v3'),
        -- =================================================================
        -- CASH & INDUSTRIAL CROPS
        -- =================================================================
        -- Coffee Season A (perennial)
        ('coffee', 'A', 'full_season', 'rainfall_cumulative', 'below', 130.0, 1.0, 'Season cumulative rainfall below 130mm', 'Parametric'),
        ('coffee', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('coffee', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('coffee', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('coffee', 'A', 'full_season', 'et_anomaly', 'below', -18.0, 0.5, 'ET anomaly exceeds -18% deficit', 'WaPOR v3'),
        -- Tea Season A (perennial, high moisture)
        ('tea', 'A', 'full_season', 'rainfall_cumulative', 'below', 150.0, 1.0, 'Season cumulative rainfall below 150mm', 'Parametric'),
        ('tea', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('tea', 'A', 'full_season', 'dry_spell_days', 'above', 10.0, 0.8, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('tea', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('tea', 'A', 'full_season', 'soil_moisture', 'below', 30.0, 0.6, 'Soil moisture below 30%', 'WaPOR v3'),
        -- Sugarcane Season A & B (high water needs)
        ('sugarcane', 'A', 'full_season', 'rainfall_cumulative', 'below', 140.0, 1.0, 'Season cumulative rainfall below 140mm', 'Parametric'),
        ('sugarcane', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('sugarcane', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('sugarcane', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('sugarcane', 'A', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        ('sugarcane', 'B', 'full_season', 'rainfall_cumulative', 'below', 140.0, 1.0, 'Season cumulative rainfall below 140mm', 'Parametric'),
        ('sugarcane', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('sugarcane', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('sugarcane', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('sugarcane', 'B', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        -- Pyrethrum Season A & B
        ('pyrethrum', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('pyrethrum', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('pyrethrum', 'A', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('pyrethrum', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('pyrethrum', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        ('pyrethrum', 'B', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('pyrethrum', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('pyrethrum', 'B', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('pyrethrum', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('pyrethrum', 'B', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        -- Tobacco Season A & B
        ('tobacco', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('tobacco', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('tobacco', 'A', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('tobacco', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('tobacco', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        ('tobacco', 'B', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('tobacco', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('tobacco', 'B', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('tobacco', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('tobacco', 'B', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        -- Sunflower Season A & B
        ('sunflower', 'A', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('sunflower', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('sunflower', 'A', 'full_season', 'dry_spell_days', 'above', 18.0, 0.6, 'Maximum dry spell exceeds 18 days', 'Parametric'),
        ('sunflower', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('sunflower', 'A', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        ('sunflower', 'B', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('sunflower', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('sunflower', 'B', 'full_season', 'dry_spell_days', 'above', 18.0, 0.6, 'Maximum dry spell exceeds 18 days', 'Parametric'),
        ('sunflower', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('sunflower', 'B', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        -- Macadamia Season A (perennial)
        ('macadamia', 'A', 'full_season', 'rainfall_cumulative', 'below', 120.0, 1.0, 'Season cumulative rainfall below 120mm', 'Parametric'),
        ('macadamia', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('macadamia', 'A', 'full_season', 'dry_spell_days', 'above', 15.0, 0.6, 'Maximum dry spell exceeds 15 days', 'Parametric'),
        ('macadamia', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('macadamia', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        -- Sesame Season A & B (drought-tolerant)
        ('sesame', 'A', 'full_season', 'rainfall_cumulative', 'below', 60.0, 1.0, 'Season cumulative rainfall below 60mm', 'Parametric'),
        ('sesame', 'A', 'full_season', 'spi', 'below', -1.5, 0.8, 'SPI indicates severe drought', 'WMO standard'),
        ('sesame', 'A', 'full_season', 'dry_spell_days', 'above', 20.0, 0.5, 'Maximum dry spell exceeds 20 days', 'Parametric'),
        ('sesame', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('sesame', 'A', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        ('sesame', 'B', 'full_season', 'rainfall_cumulative', 'below', 60.0, 1.0, 'Season cumulative rainfall below 60mm', 'Parametric'),
        ('sesame', 'B', 'full_season', 'spi', 'below', -1.5, 0.8, 'SPI indicates severe drought', 'WMO standard'),
        ('sesame', 'B', 'full_season', 'dry_spell_days', 'above', 20.0, 0.5, 'Maximum dry spell exceeds 20 days', 'Parametric'),
        ('sesame', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('sesame', 'B', 'full_season', 'et_anomaly', 'below', -25.0, 0.4, 'ET anomaly exceeds -25% deficit', 'WaPOR v3'),
        -- =================================================================
        -- ADDITIONAL VEGETABLES
        -- =================================================================
        -- Leek Season A & B
        ('leek', 'A', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('leek', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('leek', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('leek', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('leek', 'A', 'full_season', 'soil_moisture', 'below', 22.0, 0.5, 'Soil moisture below 22%', 'WaPOR v3'),
        ('leek', 'B', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('leek', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('leek', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('leek', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('leek', 'B', 'full_season', 'soil_moisture', 'below', 22.0, 0.5, 'Soil moisture below 22%', 'WaPOR v3'),
        -- Lettuce Season A & B (fast-growing, moisture-sensitive)
        ('lettuce', 'A', 'full_season', 'rainfall_cumulative', 'below', 60.0, 1.0, 'Season cumulative rainfall below 60mm', 'Parametric'),
        ('lettuce', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('lettuce', 'A', 'full_season', 'dry_spell_days', 'above', 8.0, 0.8, 'Maximum dry spell exceeds 8 days', 'Parametric'),
        ('lettuce', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('lettuce', 'A', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        ('lettuce', 'B', 'full_season', 'rainfall_cumulative', 'below', 60.0, 1.0, 'Season cumulative rainfall below 60mm', 'Parametric'),
        ('lettuce', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('lettuce', 'B', 'full_season', 'dry_spell_days', 'above', 8.0, 0.8, 'Maximum dry spell exceeds 8 days', 'Parametric'),
        ('lettuce', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('lettuce', 'B', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        -- Spinach Season A & B (fast-growing)
        ('spinach', 'A', 'full_season', 'rainfall_cumulative', 'below', 55.0, 1.0, 'Season cumulative rainfall below 55mm', 'Parametric'),
        ('spinach', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('spinach', 'A', 'full_season', 'dry_spell_days', 'above', 8.0, 0.8, 'Maximum dry spell exceeds 8 days', 'Parametric'),
        ('spinach', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('spinach', 'A', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        ('spinach', 'B', 'full_season', 'rainfall_cumulative', 'below', 55.0, 1.0, 'Season cumulative rainfall below 55mm', 'Parametric'),
        ('spinach', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('spinach', 'B', 'full_season', 'dry_spell_days', 'above', 8.0, 0.8, 'Maximum dry spell exceeds 8 days', 'Parametric'),
        ('spinach', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('spinach', 'B', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        -- Cucumber Season A & B
        ('cucumber', 'A', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('cucumber', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('cucumber', 'A', 'full_season', 'dry_spell_days', 'above', 8.0, 0.8, 'Maximum dry spell exceeds 8 days', 'Parametric'),
        ('cucumber', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('cucumber', 'A', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        ('cucumber', 'B', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('cucumber', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('cucumber', 'B', 'full_season', 'dry_spell_days', 'above', 8.0, 0.8, 'Maximum dry spell exceeds 8 days', 'Parametric'),
        ('cucumber', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('cucumber', 'B', 'full_season', 'soil_moisture', 'below', 25.0, 0.6, 'Soil moisture below 25%', 'WaPOR v3'),
        -- Watermelon Season A & B
        ('watermelon', 'A', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('watermelon', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('watermelon', 'A', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('watermelon', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('watermelon', 'A', 'full_season', 'soil_moisture', 'below', 22.0, 0.5, 'Soil moisture below 22%', 'WaPOR v3'),
        ('watermelon', 'B', 'full_season', 'rainfall_cumulative', 'below', 80.0, 1.0, 'Season cumulative rainfall below 80mm', 'Parametric'),
        ('watermelon', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('watermelon', 'B', 'full_season', 'dry_spell_days', 'above', 10.0, 0.7, 'Maximum dry spell exceeds 10 days', 'Parametric'),
        ('watermelon', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('watermelon', 'B', 'full_season', 'soil_moisture', 'below', 22.0, 0.5, 'Soil moisture below 22%', 'WaPOR v3'),
        -- Pumpkin Season A & B
        ('pumpkin', 'A', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('pumpkin', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('pumpkin', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('pumpkin', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('pumpkin', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        ('pumpkin', 'B', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('pumpkin', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('pumpkin', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('pumpkin', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('pumpkin', 'B', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        -- =================================================================
        -- ADDITIONAL FRUITS
        -- =================================================================
        -- Guava Season A (perennial)
        ('guava', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('guava', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('guava', 'A', 'full_season', 'dry_spell_days', 'above', 18.0, 0.6, 'Maximum dry spell exceeds 18 days', 'Parametric'),
        ('guava', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('guava', 'A', 'full_season', 'et_anomaly', 'below', -22.0, 0.4, 'ET anomaly exceeds -22% deficit', 'WaPOR v3'),
        -- Cape gooseberry Season A & B
        ('cape_gooseberry', 'A', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('cape_gooseberry', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('cape_gooseberry', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('cape_gooseberry', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('cape_gooseberry', 'A', 'full_season', 'et_anomaly', 'below', -18.0, 0.4, 'ET anomaly exceeds -18% deficit', 'WaPOR v3'),
        ('cape_gooseberry', 'B', 'full_season', 'rainfall_cumulative', 'below', 100.0, 1.0, 'Season cumulative rainfall below 100mm', 'Parametric'),
        ('cape_gooseberry', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('cape_gooseberry', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('cape_gooseberry', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('cape_gooseberry', 'B', 'full_season', 'et_anomaly', 'below', -18.0, 0.4, 'ET anomaly exceeds -18% deficit', 'WaPOR v3'),
        -- =================================================================
        -- ADDITIONAL OIL/INDUSTRIAL CROPS
        -- =================================================================
        -- Oil palm Season A (perennial)
        ('oil_palm', 'A', 'full_season', 'rainfall_cumulative', 'below', 140.0, 1.0, 'Season cumulative rainfall below 140mm', 'Parametric'),
        ('oil_palm', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('oil_palm', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('oil_palm', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('oil_palm', 'A', 'full_season', 'soil_moisture', 'below', 28.0, 0.6, 'Soil moisture below 28%', 'WaPOR v3'),
        -- Soya Season A & B (alias for soybean)
        ('soya', 'A', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('soya', 'A', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('soya', 'A', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('soya', 'A', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('soya', 'A', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),
        ('soya', 'B', 'full_season', 'rainfall_cumulative', 'below', 90.0, 1.0, 'Season cumulative rainfall below 90mm', 'Parametric'),
        ('soya', 'B', 'full_season', 'spi', 'below', -1.0, 0.8, 'SPI indicates moderate drought', 'WMO standard'),
        ('soya', 'B', 'full_season', 'dry_spell_days', 'above', 12.0, 0.7, 'Maximum dry spell exceeds 12 days', 'Parametric'),
        ('soya', 'B', 'full_season', 'ndvi_z_score', 'below', -1.5, 0.8, 'NDVI anomaly indicates severe vegetation stress', 'Sentinel-2/SAR'),
        ('soya', 'B', 'full_season', 'et_anomaly', 'below', -20.0, 0.4, 'ET anomaly exceeds -20% deficit', 'WaPOR v3'),

        -- =================================================================
        -- SAR BACKSCATTER TRIGGERS (Sentinel-1 VH/VV ratio)
        -- Cloud-penetrating vegetation density signal, critical for Rwanda
        -- =================================================================

        -- Cereals & grains (moderate canopy, threshold 0.15)
        ('maize', 'A', 'full_season', 'sar_backscatter', 'below', 0.15, 0.7, 'SAR VH/VV below 0.15 — low vegetation density', 'Sentinel-1 SAR'),
        ('maize', 'B', 'full_season', 'sar_backscatter', 'below', 0.15, 0.7, 'SAR VH/VV below 0.15 — low vegetation density', 'Sentinel-1 SAR'),
        ('sorghum', 'A', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('sorghum', 'B', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('wheat', 'A', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('finger_millet', 'A', 'full_season', 'sar_backscatter', 'below', 0.12, 0.7, 'SAR VH/VV below 0.12 — low vegetation density', 'Sentinel-1 SAR'),
        ('finger_millet', 'B', 'full_season', 'sar_backscatter', 'below', 0.12, 0.7, 'SAR VH/VV below 0.12 — low vegetation density', 'Sentinel-1 SAR'),
        ('rice', 'A', 'full_season', 'sar_backscatter', 'below', 0.18, 0.7, 'SAR VH/VV below 0.18 — low vegetation density', 'Sentinel-1 SAR'),
        ('rice', 'B', 'full_season', 'sar_backscatter', 'below', 0.18, 0.7, 'SAR VH/VV below 0.18 — low vegetation density', 'Sentinel-1 SAR'),

        -- Tubers & roots (moderate-low canopy)
        ('potato', 'A', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('potato', 'B', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('sweet_potato', 'A', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('sweet_potato', 'B', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('cassava', 'A', 'full_season', 'sar_backscatter', 'below', 0.12, 0.7, 'SAR VH/VV below 0.12 — low vegetation density', 'Sentinel-1 SAR'),
        ('cassava', 'B', 'full_season', 'sar_backscatter', 'below', 0.12, 0.7, 'SAR VH/VV below 0.12 — low vegetation density', 'Sentinel-1 SAR'),
        ('yam', 'A', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('yam', 'B', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('taro', 'A', 'full_season', 'sar_backscatter', 'below', 0.16, 0.7, 'SAR VH/VV below 0.16 — low vegetation density', 'Sentinel-1 SAR'),
        ('taro', 'B', 'full_season', 'sar_backscatter', 'below', 0.16, 0.7, 'SAR VH/VV below 0.16 — low vegetation density', 'Sentinel-1 SAR'),

        -- Legumes (moderate canopy)
        ('beans', 'A', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('beans', 'B', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('soybean', 'A', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('soybean', 'B', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('groundnut', 'A', 'full_season', 'sar_backscatter', 'below', 0.12, 0.7, 'SAR VH/VV below 0.12 — low vegetation density', 'Sentinel-1 SAR'),
        ('groundnut', 'B', 'full_season', 'sar_backscatter', 'below', 0.12, 0.7, 'SAR VH/VV below 0.12 — low vegetation density', 'Sentinel-1 SAR'),
        ('peas', 'A', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('peas', 'B', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('cowpea', 'A', 'full_season', 'sar_backscatter', 'below', 0.11, 0.7, 'SAR VH/VV below 0.11 — low vegetation density', 'Sentinel-1 SAR'),
        ('cowpea', 'B', 'full_season', 'sar_backscatter', 'below', 0.11, 0.7, 'SAR VH/VV below 0.11 — low vegetation density', 'Sentinel-1 SAR'),
        ('pigeon_pea', 'A', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('pigeon_pea', 'B', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),

        -- Vegetables (moderate-low canopy)
        ('tomato', 'A', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('tomato', 'B', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('onion', 'A', 'full_season', 'sar_backscatter', 'below', 0.10, 0.7, 'SAR VH/VV below 0.10 — low vegetation density', 'Sentinel-1 SAR'),
        ('onion', 'B', 'full_season', 'sar_backscatter', 'below', 0.10, 0.7, 'SAR VH/VV below 0.10 — low vegetation density', 'Sentinel-1 SAR'),
        ('cabbage', 'A', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('cabbage', 'B', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('carrot', 'A', 'full_season', 'sar_backscatter', 'below', 0.11, 0.7, 'SAR VH/VV below 0.11 — low vegetation density', 'Sentinel-1 SAR'),
        ('carrot', 'B', 'full_season', 'sar_backscatter', 'below', 0.11, 0.7, 'SAR VH/VV below 0.11 — low vegetation density', 'Sentinel-1 SAR'),
        ('chili', 'A', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('chili', 'B', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('eggplant', 'A', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('eggplant', 'B', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('green_pepper', 'A', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('green_pepper', 'B', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('garlic', 'A', 'full_season', 'sar_backscatter', 'below', 0.10, 0.7, 'SAR VH/VV below 0.10 — low vegetation density', 'Sentinel-1 SAR'),
        ('garlic', 'B', 'full_season', 'sar_backscatter', 'below', 0.10, 0.7, 'SAR VH/VV below 0.10 — low vegetation density', 'Sentinel-1 SAR'),
        ('amaranth', 'A', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('amaranth', 'B', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('leek', 'A', 'full_season', 'sar_backscatter', 'below', 0.11, 0.7, 'SAR VH/VV below 0.11 — low vegetation density', 'Sentinel-1 SAR'),
        ('leek', 'B', 'full_season', 'sar_backscatter', 'below', 0.11, 0.7, 'SAR VH/VV below 0.11 — low vegetation density', 'Sentinel-1 SAR'),
        ('lettuce', 'A', 'full_season', 'sar_backscatter', 'below', 0.11, 0.7, 'SAR VH/VV below 0.11 — low vegetation density', 'Sentinel-1 SAR'),
        ('lettuce', 'B', 'full_season', 'sar_backscatter', 'below', 0.11, 0.7, 'SAR VH/VV below 0.11 — low vegetation density', 'Sentinel-1 SAR'),
        ('spinach', 'A', 'full_season', 'sar_backscatter', 'below', 0.12, 0.7, 'SAR VH/VV below 0.12 — low vegetation density', 'Sentinel-1 SAR'),
        ('spinach', 'B', 'full_season', 'sar_backscatter', 'below', 0.12, 0.7, 'SAR VH/VV below 0.12 — low vegetation density', 'Sentinel-1 SAR'),
        ('cucumber', 'A', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('cucumber', 'B', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('watermelon', 'A', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('watermelon', 'B', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('pumpkin', 'A', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('pumpkin', 'B', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),

        -- Fruits (high canopy — perennials have higher thresholds)
        ('banana', 'A', 'full_season', 'sar_backscatter', 'below', 0.20, 0.7, 'SAR VH/VV below 0.20 — low vegetation density', 'Sentinel-1 SAR'),
        ('banana', 'B', 'full_season', 'sar_backscatter', 'below', 0.20, 0.7, 'SAR VH/VV below 0.20 — low vegetation density', 'Sentinel-1 SAR'),
        ('avocado', 'A', 'full_season', 'sar_backscatter', 'below', 0.22, 0.7, 'SAR VH/VV below 0.22 — low canopy density', 'Sentinel-1 SAR'),
        ('mango', 'A', 'full_season', 'sar_backscatter', 'below', 0.20, 0.7, 'SAR VH/VV below 0.20 — low canopy density', 'Sentinel-1 SAR'),
        ('passion_fruit', 'A', 'full_season', 'sar_backscatter', 'below', 0.15, 0.7, 'SAR VH/VV below 0.15 — low vegetation density', 'Sentinel-1 SAR'),
        ('passion_fruit', 'B', 'full_season', 'sar_backscatter', 'below', 0.15, 0.7, 'SAR VH/VV below 0.15 — low vegetation density', 'Sentinel-1 SAR'),
        ('pineapple', 'A', 'full_season', 'sar_backscatter', 'below', 0.12, 0.7, 'SAR VH/VV below 0.12 — low vegetation density', 'Sentinel-1 SAR'),
        ('papaya', 'A', 'full_season', 'sar_backscatter', 'below', 0.16, 0.7, 'SAR VH/VV below 0.16 — low canopy density', 'Sentinel-1 SAR'),
        ('papaya', 'B', 'full_season', 'sar_backscatter', 'below', 0.16, 0.7, 'SAR VH/VV below 0.16 — low canopy density', 'Sentinel-1 SAR'),
        ('citrus', 'A', 'full_season', 'sar_backscatter', 'below', 0.20, 0.7, 'SAR VH/VV below 0.20 — low canopy density', 'Sentinel-1 SAR'),
        ('strawberry', 'A', 'full_season', 'sar_backscatter', 'below', 0.11, 0.7, 'SAR VH/VV below 0.11 — low vegetation density', 'Sentinel-1 SAR'),
        ('strawberry', 'B', 'full_season', 'sar_backscatter', 'below', 0.11, 0.7, 'SAR VH/VV below 0.11 — low vegetation density', 'Sentinel-1 SAR'),
        ('tree_tomato', 'A', 'full_season', 'sar_backscatter', 'below', 0.16, 0.7, 'SAR VH/VV below 0.16 — low canopy density', 'Sentinel-1 SAR'),
        ('tree_tomato', 'B', 'full_season', 'sar_backscatter', 'below', 0.16, 0.7, 'SAR VH/VV below 0.16 — low canopy density', 'Sentinel-1 SAR'),
        ('guava', 'A', 'full_season', 'sar_backscatter', 'below', 0.18, 0.7, 'SAR VH/VV below 0.18 — low canopy density', 'Sentinel-1 SAR'),
        ('cape_gooseberry', 'A', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('cape_gooseberry', 'B', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),

        -- Cash & industrial crops (dense canopy for tree crops)
        ('coffee', 'A', 'full_season', 'sar_backscatter', 'below', 0.22, 0.7, 'SAR VH/VV below 0.22 — low canopy density', 'Sentinel-1 SAR'),
        ('tea', 'A', 'full_season', 'sar_backscatter', 'below', 0.20, 0.7, 'SAR VH/VV below 0.20 — low canopy density', 'Sentinel-1 SAR'),
        ('sugarcane', 'A', 'full_season', 'sar_backscatter', 'below', 0.20, 0.7, 'SAR VH/VV below 0.20 — low vegetation density', 'Sentinel-1 SAR'),
        ('sugarcane', 'B', 'full_season', 'sar_backscatter', 'below', 0.20, 0.7, 'SAR VH/VV below 0.20 — low vegetation density', 'Sentinel-1 SAR'),
        ('pyrethrum', 'A', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('pyrethrum', 'B', 'full_season', 'sar_backscatter', 'below', 0.13, 0.7, 'SAR VH/VV below 0.13 — low vegetation density', 'Sentinel-1 SAR'),
        ('tobacco', 'A', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('tobacco', 'B', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('sunflower', 'A', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('sunflower', 'B', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('macadamia', 'A', 'full_season', 'sar_backscatter', 'below', 0.22, 0.7, 'SAR VH/VV below 0.22 — low canopy density', 'Sentinel-1 SAR'),
        ('sesame', 'A', 'full_season', 'sar_backscatter', 'below', 0.11, 0.7, 'SAR VH/VV below 0.11 — low vegetation density', 'Sentinel-1 SAR'),
        ('sesame', 'B', 'full_season', 'sar_backscatter', 'below', 0.11, 0.7, 'SAR VH/VV below 0.11 — low vegetation density', 'Sentinel-1 SAR'),
        ('oil_palm', 'A', 'full_season', 'sar_backscatter', 'below', 0.22, 0.7, 'SAR VH/VV below 0.22 — low canopy density', 'Sentinel-1 SAR'),
        ('soya', 'A', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR'),
        ('soya', 'B', 'full_season', 'sar_backscatter', 'below', 0.14, 0.7, 'SAR VH/VV below 0.14 — low vegetation density', 'Sentinel-1 SAR');
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_insurance_triggers_updated_at ON insurance_triggers")
    op.execute("DROP FUNCTION IF EXISTS update_insurance_triggers_timestamp()")
    op.drop_table("insurance_triggers")
