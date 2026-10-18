-- ==============================================================================
-- 03_staging_crm_seed.sql
-- Sets up the target enterprise CRM business tables and seeds realistic "dirty"
-- EU client records for the AI cleaner agent to detect, standardize, and govern.
-- ==============================================================================

\connect staging_db;

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 1. Clients Table (Business CRM)
CREATE TABLE IF NOT EXISTS clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    phone VARCHAR(50),
    billing_address TEXT,
    postal_code VARCHAR(20),
    city VARCHAR(100),
    country_code VARCHAR(20),       -- Messy: 'Deutschland', 'germany', 'DE', 'FR', 'France', etc.
    gdpr_consent BOOLEAN DEFAULT FALSE,
    consent_date DATE,
    status VARCHAR(50) DEFAULT 'PENDING',
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Invoices Table (Financial Records)
CREATE TABLE IF NOT EXISTS invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID REFERENCES clients(id) ON DELETE CASCADE,
    invoice_number VARCHAR(100) NOT NULL,
    amount_raw VARCHAR(50),         -- Messy: "€ 1.250,50", "3400.00 EUR", "  $ 450.99  "
    amount_standardized NUMERIC(12, 2),
    currency VARCHAR(10),
    invoice_date DATE NOT NULL,
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Seed Realistic "Dirty" EU Client Records
INSERT INTO clients (id, full_name, email, phone, billing_address, postal_code, city, country_code, gdpr_consent, consent_date, status)
VALUES
    -- Client 1: Germany (Inconsistent Country Name, Non-standard Phone, Messy Case)
    ('a1111111-1111-1111-1111-111111111111', 'Hans Gruber', 'HANS.GRUBER@Acme-Gmbh.DE ', '+49 (0) 89 123456-0', 'Sonnenstrasse 14b', '80331', 'münchen', 'Deutschland', FALSE, NULL, 'PENDING'),
    
    -- Client 2: Germany (Numeric Prefix Phone, Country 'DE', Upper Case Address)
    ('a2222222-2222-2222-2222-222222222222', 'Klara Schneider', 'klara_schneider@web.de', '0049 171 9876543', 'BERLINER ALLEE 45', 'D-40212', 'Dusseldorf', 'DE', TRUE, '2025-01-15', 'PENDING'),
    
    -- Client 3: Germany (Country 'germany' lowercase, unstandardized phone)
    ('a3333333-3333-3333-3333-333333333333', 'Maximilian Weber', 'max.weber@tech-startup.io', '089/987654', 'Marienplatz 1', '80331', 'Munich', 'germany', FALSE, NULL, 'PENDING'),
    
    -- Client 4: France (Country 'France', Local French phone format, missing postal code format)
    ('a4444444-4444-4444-4444-444444444444', 'Jean-Luc Dupont', 'jean.dupont@logistics-fr.com', '01 42 68 55 00', '12 Rue de la Paix', '75002', 'paris', 'France', TRUE, '2024-11-20', 'PENDING'),
    
    -- Client 5: France (Country 'FR', international format with dashes)
    ('a5555555-5555-5555-5555-555555555555', 'Amélie Moreau', ' amelie.moreau@paris-fashion.fr', '+33-1-42-68-55-11', '55 Avenue des Champs-Élysées', '75008', 'Paris', 'FR', FALSE, NULL, 'PENDING'),
    
    -- Client 6: Spain (Country 'España', trailing spaces in email, messy phone)
    ('a6666666-6666-6666-6666-666666666666', 'Carlos Ruiz', 'carlos.ruiz@madrid-digital.es   ', '+34 91 123 45 67', 'Gran Via 28, 4th floor', '28013', 'madrid', 'España', TRUE, '2025-02-10', 'PENDING'),
    
    -- Client 7: Spain (Country 'ES', missing international prefix)
    ('a7777777-7777-7777-7777-777777777777', 'Elena Gomez', 'elena.gomez@barcelona-biotech.com', '612 345 678', 'Passeig de Gracia 92', '08008', 'Barcelona', 'ES', FALSE, NULL, 'PENDING'),
    
    -- Client 8: Italy (Country 'italy' lowercase, malformed phone, missing consent)
    ('a8888888-8888-8888-8888-888888888888', 'Marco Rossi', 'MARCO.ROSSI@MILANO-DESIGN.IT', '0039 02 889911', 'Via Montenapoleone 8', '20121', 'milano', 'italy', FALSE, NULL, 'PENDING'),
    
    -- Client 9: Netherlands (Country 'The Netherlands', phone with brackets)
    ('a9999999-9999-9999-9999-999999999999', 'Sander van der Berg', 'sander@amsterdam-ventures.nl', '+31 (20) 555-0199', 'Keizersgracht 421', '1016 EK', 'Amsterdam', 'The Netherlands', TRUE, '2025-03-01', 'PENDING'),
    
    -- Client 10: Non-EU Client (United States) to test filtering / scoping constraints
    ('b1111111-1111-1111-1111-111111111111', 'John Doe', 'john.doe@ny-financial.com', '+1 (212) 555-0143', '100 Wall Street', '10005', 'New York', 'US', TRUE, '2024-05-10', 'ACTIVE')
ON CONFLICT (id) DO NOTHING;

-- Seed Invoices with Messy Formats
INSERT INTO invoices (id, client_id, invoice_number, amount_raw, amount_standardized, currency, invoice_date)
VALUES
    ('c1111111-1111-1111-1111-111111111111', 'a1111111-1111-1111-1111-111111111111', 'INV-2025-001', '€ 1.250,50', NULL, NULL, '2025-01-10'),
    ('c2222222-2222-2222-2222-222222222222', 'a2222222-2222-2222-2222-222222222222', 'INV-2025-002', '3.400,00 EUR', NULL, NULL, '2025-01-12'),
    ('c3333333-3333-3333-3333-333333333333', 'a4444444-4444-4444-4444-444444444444', 'INV-2025-003', '  € 850,75  ', NULL, NULL, '2025-01-15'),
    ('c4444444-4444-4444-4444-444444444444', 'a6666666-6666-6666-6666-666666666666', 'INV-2025-004', '4200.00 EUR', NULL, NULL, '2025-02-01'),
    ('c5555555-5555-5555-5555-555555555555', 'b1111111-1111-1111-1111-111111111111', 'INV-2025-005', '$ 5,500.00', NULL, NULL, '2025-02-05')
ON CONFLICT (id) DO NOTHING;
