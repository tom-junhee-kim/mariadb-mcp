-- 메모리 아틀라스 10단계 — 규칙·배경·지식의 정본 표 7개.
-- {S} 는 표 접미사(운영은 빈 값, 시험은 _test). 적용: python src/atlas_docs.py --print-ddl _test | mariadb …
-- 키 칸은 utf8mb4_bin, 시각은 UTC DATETIME(3).

CREATE TABLE IF NOT EXISTS docs{S} (
  doc_id        VARCHAR(400) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
  kind          ENUM('rule','guide','machine_doc','workspace_memory') NOT NULL,
  scope         VARCHAR(160) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  name          VARCHAR(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  status        ENUM('active','retired') NOT NULL DEFAULT 'active',
  header        LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL CHECK (JSON_VALID(header)),
  body          MEDIUMTEXT NOT NULL,
  body_sha      CHAR(16) NOT NULL,
  version       INT UNSIGNED NOT NULL,
  chunk_version INT UNSIGNED NOT NULL DEFAULT 0,
  updated_at    DATETIME(3) NOT NULL,
  updated_by    VARCHAR(120) NOT NULL,
  UNIQUE KEY uq_doc (kind, scope, name),
  KEY ix_scope (scope, kind, status),
  KEY ix_chunk_lag (chunk_version, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS docs_history{S} (
  doc_id      VARCHAR(400) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  version     INT UNSIGNED NOT NULL,
  header      LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  body        MEDIUMTEXT NOT NULL,
  status      VARCHAR(16) NOT NULL,
  updated_at  DATETIME(3) NOT NULL,
  updated_by  VARCHAR(120) NOT NULL,
  reason      TEXT NULL,
  proposal_id BIGINT UNSIGNED NULL,
  PRIMARY KEY (doc_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS doc_proposals{S} (
  proposal_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  op             ENUM('create','update','move','retire') NOT NULL,
  doc_id         VARCHAR(400) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  kind           ENUM('rule','guide','machine_doc','workspace_memory') NOT NULL,
  scope          VARCHAR(160) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  name           VARCHAR(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  to_scope       VARCHAR(160) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
  base_version   INT UNSIGNED NULL,
  header         LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
  body           MEDIUMTEXT NULL,
  reason         TEXT NOT NULL,
  proposed_by    VARCHAR(120) NOT NULL,
  proposed_at    DATETIME(3) NOT NULL,
  test_sha       CHAR(16) NULL,
  state          ENUM('open','approved','rejected') NOT NULL DEFAULT 'open',
  decided_by     VARCHAR(120) NULL,
  approval_quote TEXT NULL,
  standing_id    BIGINT UNSIGNED NULL,
  decided_at     DATETIME(3) NULL,
  KEY ix_state (state, proposal_id),
  KEY ix_doc (doc_id, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rule_events{S} (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  at           DATETIME(3) NOT NULL,
  machine      VARCHAR(32) NOT NULL,
  session_id   CHAR(36) NOT NULL,
  session_name VARCHAR(255) NULL,
  workspace    VARCHAR(120) NULL,
  event_name   VARCHAR(24) NOT NULL,
  tool         VARCHAR(120) NULL,
  doc_id       VARCHAR(400) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
  decision     VARCHAR(24) NOT NULL,
  subject_sha  CHAR(8) NULL,
  input_sha    CHAR(8) NULL,
  distance     FLOAT NULL,
  reaction     ENUM('changed','checked-rerun','instant-rerun','none') NULL,
  event_uid    CHAR(40) NOT NULL,
  UNIQUE KEY uq_event (event_uid),
  KEY ix_doc (doc_id, at),
  KEY ix_session (session_id, at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tuning{S} (
  k          VARCHAR(80) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
  v          DOUBLE NOT NULL,
  lo         DOUBLE NULL,
  hi         DOUBLE NULL,
  updated_at DATETIME(3) NOT NULL,
  reason     TEXT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ws_cutover{S} (
  workspace   VARCHAR(120) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  machine     VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  mode        ENUM('file','db') NOT NULL,
  switched_at DATETIME(3) NOT NULL,
  switched_by VARCHAR(120) NOT NULL,
  PRIMARY KEY (workspace, machine)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS standing_approvals{S} (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  scope_desc    TEXT NOT NULL,
  match_rule    LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL CHECK (JSON_VALID(match_rule)),
  quote         TEXT NOT NULL,
  quote_session CHAR(36) NOT NULL,
  quote_line    INT NOT NULL,
  approver      VARCHAR(40) NOT NULL,
  created_at    DATETIME(3) NOT NULL,
  expires_at    DATETIME(3) NULL,
  revoked       TINYINT NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO tuning{S} (k, v, lo, hi, updated_at, reason) VALUES
  ('rules_version', 0, NULL, NULL, UTC_TIMESTAMP(3), '초기값 — docs·doc_proposals 반영 때마다 +1'),
  ('tuning_version', 0, NULL, NULL, UTC_TIMESTAMP(3), '초기값 — set_tuning 때마다 +1'),
  ('D:prompt', 0.48, 0.40, 0.52, UTC_TIMESTAMP(3), '10-23 M-57 거리 상한'),
  ('D:tool', 0.44, 0.40, 0.52, UTC_TIMESTAMP(3), '시작값 — 4단계 판정 표본으로 다시 정함');
