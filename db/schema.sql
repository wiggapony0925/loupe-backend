-- loupe-backend Postgres schema (auto-generated from app.db.Base.metadata).
-- Reflects the same DDL as alembic upgrade head against a Postgres target.

CREATE TABLE card_sets (
	id UUID NOT NULL, 
	tcg tcg_enum NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	code VARCHAR(40), 
	release_date DATE, 
	total_cards INTEGER, 
	image_url VARCHAR(1024), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_card_sets PRIMARY KEY (id)
);
CREATE INDEX ix_card_sets_code ON card_sets (code);
CREATE INDEX ix_card_sets_tcg ON card_sets (tcg);

CREATE TABLE users (
	id UUID NOT NULL, 
	email VARCHAR(320) NOT NULL, 
	display_name VARCHAR(120), 
	avatar_url VARCHAR(1024), 
	apple_subject VARCHAR(255), 
	google_subject VARCHAR(255), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	deleted_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_users PRIMARY KEY (id), 
	CONSTRAINT uq_users_apple_subject UNIQUE (apple_subject), 
	CONSTRAINT uq_users_google_subject UNIQUE (google_subject)
);
CREATE UNIQUE INDEX ix_users_email ON users (email);

CREATE TABLE api_keys (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	key_hash VARCHAR(128) NOT NULL, 
	last_used_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_api_keys PRIMARY KEY (id), 
	CONSTRAINT fk_api_keys_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	CONSTRAINT uq_api_keys_key_hash UNIQUE (key_hash)
);
CREATE INDEX ix_api_keys_user_id ON api_keys (user_id);

CREATE TABLE audit_log (
	id UUID NOT NULL, 
	user_id UUID, 
	action VARCHAR(80) NOT NULL, 
	target_table VARCHAR(80), 
	target_id VARCHAR(64), 
	payload JSON, 
	ip_address VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_audit_log PRIMARY KEY (id), 
	CONSTRAINT fk_audit_log_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_audit_log_user_id ON audit_log (user_id);
CREATE INDEX ix_audit_log_action ON audit_log (action);

CREATE TABLE cards (
	id UUID NOT NULL, 
	set_id UUID NOT NULL, 
	tcg tcg_enum NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	number VARCHAR(40), 
	rarity VARCHAR(60), 
	year INTEGER, 
	image_url VARCHAR(1024), 
	metadata JSON, 
	image_phash VARCHAR(64), 
	image_dhash VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_cards PRIMARY KEY (id), 
	CONSTRAINT fk_cards_set_id_card_sets FOREIGN KEY(set_id) REFERENCES card_sets (id) ON DELETE CASCADE
);
CREATE INDEX ix_cards_tcg_name ON cards (tcg, name);
CREATE INDEX ix_cards_set_id ON cards (set_id);
CREATE INDEX ix_cards_tcg ON cards (tcg);
CREATE INDEX ix_cards_image_phash ON cards (image_phash);
CREATE INDEX ix_cards_image_dhash ON cards (image_dhash);

CREATE TABLE collections (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	description VARCHAR(500), 
	color VARCHAR(16), 
	is_public BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_collections PRIMARY KEY (id), 
	CONSTRAINT fk_collections_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX ix_collections_user_id ON collections (user_id);

CREATE TABLE scanners (
	id UUID NOT NULL, 
	owner_id UUID NOT NULL, 
	device_id VARCHAR(128) NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	firmware_version VARCHAR(32), 
	last_seen_at TIMESTAMP WITH TIME ZONE, 
	transport scanner_transport_enum NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_scanners PRIMARY KEY (id), 
	CONSTRAINT fk_scanners_owner_id_users FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE, 
	CONSTRAINT uq_scanners_device_id UNIQUE (device_id)
);
CREATE INDEX ix_scanners_owner_id ON scanners (owner_id);

CREATE TABLE user_settings (
	user_id UUID NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	theme VARCHAR(16) NOT NULL, 
	live_sync_enabled BOOLEAN NOT NULL, 
	push_notifications_enabled BOOLEAN NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_user_settings PRIMARY KEY (user_id), 
	CONSTRAINT fk_user_settings_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE price_snapshots (
	id UUID NOT NULL, 
	card_id UUID NOT NULL, 
	house grade_house_enum NOT NULL, 
	grade NUMERIC(4, 1) NOT NULL, 
	source price_source_enum NOT NULL, 
	price_usd NUMERIC(10, 2) NOT NULL, 
	sale_date DATE, 
	raw_payload JSON, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_price_snapshots PRIMARY KEY (id), 
	CONSTRAINT fk_price_snapshots_card_id_cards FOREIGN KEY(card_id) REFERENCES cards (id) ON DELETE CASCADE
);
CREATE INDEX ix_price_snapshots_card_id ON price_snapshots (card_id);
CREATE INDEX ix_price_snapshots_card_grade_date ON price_snapshots (card_id, grade, sale_date);

CREATE TABLE scan_jobs (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	scanner_id UUID, 
	status scan_status_enum NOT NULL, 
	source scan_source_enum NOT NULL, 
	images_s3_keys JSON, 
	error_message VARCHAR(1024), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_scan_jobs PRIMARY KEY (id), 
	CONSTRAINT fk_scan_jobs_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	CONSTRAINT fk_scan_jobs_scanner_id_scanners FOREIGN KEY(scanner_id) REFERENCES scanners (id) ON DELETE SET NULL
);
CREATE INDEX ix_scan_jobs_scanner_id ON scan_jobs (scanner_id);
CREATE INDEX ix_scan_jobs_user_id ON scan_jobs (user_id);
CREATE INDEX ix_scan_jobs_status ON scan_jobs (status);
CREATE INDEX ix_scan_jobs_user_status_created ON scan_jobs (user_id, status, created_at);

CREATE TABLE graded_cards (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	card_id UUID NOT NULL, 
	scan_job_id UUID, 
	grade NUMERIC(4, 1) NOT NULL, 
	house grade_house_enum NOT NULL, 
	subgrades JSON, 
	estimated_value_usd NUMERIC(10, 2), 
	fingerprint_hash VARCHAR(128), 
	notes VARCHAR(2000), 
	graded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	deleted_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_graded_cards PRIMARY KEY (id), 
	CONSTRAINT fk_graded_cards_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	CONSTRAINT fk_graded_cards_card_id_cards FOREIGN KEY(card_id) REFERENCES cards (id) ON DELETE RESTRICT, 
	CONSTRAINT fk_graded_cards_scan_job_id_scan_jobs FOREIGN KEY(scan_job_id) REFERENCES scan_jobs (id) ON DELETE SET NULL
);
CREATE INDEX ix_graded_cards_scan_job_id ON graded_cards (scan_job_id);
CREATE INDEX ix_graded_cards_card_id ON graded_cards (card_id);
CREATE INDEX ix_graded_cards_user_graded_at ON graded_cards (user_id, graded_at);
CREATE INDEX ix_graded_cards_fingerprint_hash ON graded_cards (fingerprint_hash);
CREATE INDEX ix_graded_cards_user_id ON graded_cards (user_id);

CREATE TABLE collection_items (
	collection_id UUID NOT NULL, 
	graded_card_id UUID NOT NULL, 
	added_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_collection_items PRIMARY KEY (collection_id, graded_card_id), 
	CONSTRAINT fk_collection_items_collection_id_collections FOREIGN KEY(collection_id) REFERENCES collections (id) ON DELETE CASCADE, 
	CONSTRAINT fk_collection_items_graded_card_id_graded_cards FOREIGN KEY(graded_card_id) REFERENCES graded_cards (id) ON DELETE CASCADE
);

CREATE TABLE fingerprints (
	id UUID NOT NULL, 
	graded_card_id UUID NOT NULL, 
	phash VARCHAR(64), 
	dhash VARCHAR(64), 
	feature_vector JSON, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_fingerprints PRIMARY KEY (id), 
	CONSTRAINT uq_fingerprints_graded_card_id UNIQUE (graded_card_id), 
	CONSTRAINT fk_fingerprints_graded_card_id_graded_cards FOREIGN KEY(graded_card_id) REFERENCES graded_cards (id) ON DELETE CASCADE
);
CREATE INDEX ix_fingerprints_phash ON fingerprints (phash);
CREATE INDEX ix_fingerprints_dhash ON fingerprints (dhash);
