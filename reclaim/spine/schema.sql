CREATE TABLE audit_log (
	sequence BIGINT NOT NULL, 
	ts TEXT NOT NULL, 
	case_id TEXT, 
	actor TEXT NOT NULL, 
	event_type TEXT NOT NULL, 
	idempotency_key TEXT, 
	prev_hash TEXT NOT NULL, 
	row_hash TEXT NOT NULL, 
	data TEXT NOT NULL, 
	PRIMARY KEY (sequence), 
	UNIQUE (row_hash)
);

CREATE TABLE obligations (
	obligation_id TEXT NOT NULL, 
	payer_id TEXT NOT NULL, 
	kind TEXT NOT NULL, 
	currency TEXT NOT NULL, 
	gross_amount_paise BIGINT NOT NULL, 
	status TEXT NOT NULL, 
	due_at TEXT NOT NULL, 
	data TEXT NOT NULL, 
	created_at TEXT NOT NULL, 
	PRIMARY KEY (obligation_id)
);

CREATE TABLE outbox (
	id BIGSERIAL NOT NULL, 
	idempotency_key TEXT NOT NULL, 
	case_id TEXT NOT NULL, 
	obligation_id TEXT, 
	attempt_sequence INTEGER, 
	action_type TEXT NOT NULL, 
	envelope TEXT NOT NULL, 
	status TEXT DEFAULT 'pending' NOT NULL, 
	claimed_by TEXT, 
	claimed_at TEXT, 
	completed_at TEXT, 
	result_digest TEXT, 
	error TEXT, 
	created_at TEXT NOT NULL, 
	updated_at TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (idempotency_key)
);

CREATE UNIQUE INDEX uq_outbox_obligation_attempt ON outbox (obligation_id, attempt_sequence) WHERE obligation_id IS NOT NULL;

CREATE TABLE risk_cases (
	case_id TEXT NOT NULL, 
	obligation_id TEXT NOT NULL, 
	payer_id TEXT NOT NULL, 
	arm TEXT NOT NULL, 
	segment TEXT NOT NULL, 
	risk_class TEXT NOT NULL, 
	amount_at_risk_paise BIGINT NOT NULL, 
	currency TEXT NOT NULL, 
	state TEXT NOT NULL, 
	stratum_key TEXT NOT NULL, 
	detected_at TEXT NOT NULL, 
	recovery_window_ends_at TEXT NOT NULL, 
	stop_reason TEXT, 
	stopped_at TEXT, 
	data TEXT NOT NULL, 
	created_at TEXT NOT NULL, 
	updated_at TEXT NOT NULL, 
	PRIMARY KEY (case_id), 
	FOREIGN KEY(obligation_id) REFERENCES obligations (obligation_id)
);

CREATE UNIQUE INDEX uq_risk_case_active_obligation ON risk_cases (obligation_id) WHERE state NOT IN ('recovered', 'stopped', 'written_off');

