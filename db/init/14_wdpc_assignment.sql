-- WDPC continuous-deployment: the disjoint ASSIGNMENT lock + the FIFO deploy QUEUE
-- (docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md §5). DESIGN-ONLY tables until the Lead activates the live
-- wiring; additive (CREATE ... IF NOT EXISTS), re-runnable on the live DB, NO data/behavior impact until the
-- applier writes to them.

-- ASSIGNMENT LOCK — each subagent owns EXACTLY ONE group (PK group_name = the one-owner-per-group lock).
-- Disjoint scopes are the conflict-preventer: two agents never touch the same file, so the entire git/merge
-- conflict class vanishes. A dead agent's lock times out via heartbeat so a group is never stuck forever.
CREATE TABLE IF NOT EXISTS within_day_assignment (
    group_name    text        NOT NULL,
    agent_id      text        NOT NULL,          -- the subagent that owns the group
    claimed_at    timestamptz NOT NULL DEFAULT now(),
    heartbeat_at  timestamptz NOT NULL DEFAULT now(),  -- the agent bumps this each cycle; a stale lock is reclaimable
    status        text        NOT NULL DEFAULT 'active',  -- 'active' | 'released' | 'timed_out'
    released_at   timestamptz,
    PRIMARY KEY (group_name)                      -- ONE active owner per group, enforced by the DB
);
CREATE INDEX IF NOT EXISTS idx_wda_agent  ON within_day_assignment (agent_id);
CREATE INDEX IF NOT EXISTS idx_wda_status ON within_day_assignment (status);

COMMENT ON TABLE within_day_assignment IS
    'WDPC disjoint single-group assignment lock (docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md §5.1). PK on '
    'group_name = one owner per group; disjoint scopes mean two agents never touch the same code, so the '
    'git-conflict class vanishes. heartbeat_at drives the dead-agent reclaim (status=timed_out).';

-- FIFO DEPLOY QUEUE — a tested + in-scope fix is enqueued; ONE serialized applier dequeues by enqueued_at
-- (FIFO), runs the scope-guard, auto-merges, hot-swaps, confirms via the bus tripwire, and on a tripwire
-- failure rolls back that one group's swap. Because assignment is disjoint, the queue needs NO merge logic.
CREATE TABLE IF NOT EXISTS within_day_deploy_queue (
    id            bigserial   PRIMARY KEY,
    group_name    text        NOT NULL,
    agent_id      text        NOT NULL,
    commit_sha    text        NOT NULL,          -- the in-scope fix commit to auto-merge + hot-swap
    enqueued_at   timestamptz NOT NULL DEFAULT now(),  -- FIFO order
    status        text        NOT NULL DEFAULT 'queued',
                  -- 'queued' | 'applying' | 'applied' | 'rolled_back' | 'escalated' | 'failed'
    started_at    timestamptz,
    finished_at   timestamptz,
    fail_count    int         NOT NULL DEFAULT 0,  -- per-group re-enqueue backoff fuel (starvation guard)
    detail        text                              -- human note (esp. escalated / rolled_back / failed)
);
CREATE INDEX IF NOT EXISTS idx_wdq_status   ON within_day_deploy_queue (status);
CREATE INDEX IF NOT EXISTS idx_wdq_fifo     ON within_day_deploy_queue (enqueued_at);
CREATE INDEX IF NOT EXISTS idx_wdq_group    ON within_day_deploy_queue (group_name);

COMMENT ON TABLE within_day_deploy_queue IS
    'WDPC FIFO deploy queue (docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md §5.2). One serialized applier '
    'dequeues by enqueued_at: scope-guard -> auto-merge -> hot_swap -> tripwire-confirm -> next; rolls back '
    'that one group on tripwire failure. Disjoint assignment makes this a plain FIFO of independent jobs '
    '(no merge logic). Serialization = no deploy/reload race.';
