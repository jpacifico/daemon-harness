// Context engine OpenClaw → daemon (Daemon/Daemon Harness).
//
// OpenClaw runs in TS/Node and the daemon in Python. This adapter contains
// no memory logic: it connects the two processes. Each hook
// calls a local FastAPI endpoint that reuses the same
// primitives as the Hermes plugin.
//
//   assemble()  → POST /openclaw/assemble → { system_prompt_addition, ... }
//   ingest()    -> POST /openclaw/ingest -> append exact dialogue; schedule maintenance
//
// The active plugin replaces the system through before_prompt_build;
// OpenClaw retains its model, streaming and tool loop. Disabling the
// plugin returns full control to the host.
//
// Config (openclaw.json → plugins.entries["daemon-harness"].config) :
//   { "daemonUrl": "http://127.0.0.1:8000", "userId": "1234567890",
//     "runtimeHealthPath": "/private/path/openclaw-runtime-health.json" }
// Environment fallback: DAEMON_HARNESS_URL, DAEMON_CORE_USER_ID and
// DAEMON_CORE_OPENCLAW_HEALTH_FILE.

import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { createHash, randomUUID } from "node:crypto";

const ENGINE_ID = "daemon-harness";
// A 20-second timeout was too close to an observed 18.8-second assembly
// through Codex subprocesses, causing silent loss of memory context.
// Ninety seconds provides headroom; hooks still fail gracefully.
const REQUEST_TIMEOUT_MS = 90000;
const TURN_CACHE_TTL_MS = 5 * 60 * 1000;
const TURN_CACHE_MAX_ENTRIES = 64;
const SOUVENIR_READ_TOOL = "daemon_read_souvenirs";
const L0_RECALL_TOOL = "daemon_recall_l0";
const OPENCLAW_HEARTBEAT_POLL = "[OpenClaw heartbeat poll]";
const OPENCLAW_HEARTBEAT_PROMPT_PREFIX = (
  "Read HEARTBEAT.md if it exists (workspace context)."
);
const OPENCLAW_CONVERSATION_INFO_MARKER = (
  "Conversation info (untrusted metadata):"
);
const OPENCLAW_ASSEMBLED_CONTEXT_MARKER = (
  "OpenClaw assembled context for this turn:"
);
const OPENCLAW_CURRENT_USER_REQUEST_MARKER = "\nCurrent user request:\n";
const CODEX_TURN_PROJECTION_VERSION = "daemon-canonical-turn-v1";
// Retained only to clean previously recorded turns.
// Daemon incidents are no longer appended to user-facing responses.
const DEGRADED_RESPONSE_NOTICE = (
  "⚠️ System notice · Personal memory was unavailable for this response."
);
const DEGRADED_HOST_CONTRACT = [
  "## Daemon fallback mode",
  "",
  "The daemon's personal memory is temporarily unavailable for this turn.",
  "Respond using the host's capabilities and the currently visible dialogue.",
  "Assert no personal fact absent from that dialogue; if such a fact is",
  "necessary, simply say you cannot verify it at the moment.",
  "Keep the available native tools and use them normally.",
].join("\n");
const OPENCLAW_PROGRESS_PREAMBLE_CONTRACT = [
  "## OpenClaw progress preamble",
  "",
  "Before the first tool, write as visible commentary a single",
  "very brief sentence in the user's language: say only what you are",
  "checking, without an unverified fact or tool name, then call it immediately.",
  "Without a tool, respond directly. Do not repeat this sentence in the final",
  "response.",
].join("\n");
const INTERNAL_TURN_TIME_LINE_RE = (
  /^[ \t]*\[heure locale du tour : \d{4}-\d{2}-\d{2}T[^\]\r\n]+\][ \t]*(?:\r?\n|$)/gm
);
const INTERNAL_TURN_COMMITMENT_LINE_RE = (
  /^[ \t]*\[daemon-turn[^\r\n]*(?:\r?\n|$)/i
);
const INTERNAL_TURN_COMMITMENT_RE = (
  /^[ \t]*\[daemon-turn:[ \t]*(exchange|direct|clarify|condition)[ \t]*\][ \t]*(.*?)[ \t]*(?:\r?\n|$)/i
);
const INTERNAL_TURN_CLARIFY_OUTPUT_RE = (
  /^[ \t]*\[daemon-turn:[ \t]*clarify[ \t]*\][ \t]*(.*?)[ \t]*(?:\r?\n[\s\S]*)?$/i
);
const INTERNAL_TURN_CONDITION_OUTPUT_RE = (
  /^[ \t]*\[daemon-turn:[ \t]*condition[ \t]*\][ \t]*/i
);
const INTERNAL_TURN_VISIBLE_OUTPUT_RE = (
  /^(?:[ \t]*\[daemon-turn:[ \t]*(?:exchange|direct)[ \t]*\][ \t]*(?:\r?\n)?)+/i
);
const OPENCLAW_TRANSIENT_TOOL_FAILURE_RE = (
  /^[ \t]*⚠️[ \t]*🛠️[ \t]+[^\r\n]+?[ \t]+failed:[\s\S]+[ \t]+\([ \t]*agent[ \t]*\)[ \t]*$/iu
);
const OPENCLAW_PROGRESS_SUMMARY_RE = (
  /^(?:(?:🧠 \d+ thoughts?|💬 \d+ notes?|🛠️ \d+ tool calls?) · )+⏱️ \d+s$/u
);
const TELEGRAM_PROGRESS_CLEANUP_DELAY_MS = 250;
const TELEGRAM_PROGRESS_DELETE_TIMEOUT_MS = 5000;
const TELEGRAM_PROGRESS_STATE_TTL_MS = 10 * 60 * 1000;
const TELEGRAM_PROGRESS_MAX_ID_SPAN = 32;
const TELEGRAM_PROGRESS_ANCHOR_PRIORITY = 10_000;
const TELEGRAM_PROGRESS_ANCHOR_DIR = join(
  process.env.OPENCLAW_STATE_DIR || join(homedir(), ".openclaw"),
  "daemon-harness",
  "telegram-progress-anchors",
);
const OPENCLAW_OUTPUT_TEXT_TRANSFORMS = [
  // Keep delivery cleanup in the provider stream itself. Registering an
  // outbound modifying hook (`message_sending` / `reply_payload_sending`)
  // makes OpenClaw disable Telegram provider previews, including the native
  // progress draft that renders tool activity.
  { from: OPENCLAW_TRANSIENT_TOOL_FAILURE_RE, to: "" },
  { from: OPENCLAW_PROGRESS_SUMMARY_RE, to: "" },
  { from: INTERNAL_TURN_TIME_LINE_RE, to: "" },
  { from: INTERNAL_TURN_CLARIFY_OUTPUT_RE, to: "$1" },
  { from: INTERNAL_TURN_CONDITION_OUTPUT_RE, to: "" },
  { from: INTERNAL_TURN_VISIBLE_OUTPUT_RE, to: "" },
  { from: INTERNAL_TURN_COMMITMENT_LINE_RE, to: "" },
];

// Session-scoped handoff from assemble/ingest to before_prompt_build.
// This process-local cache contains only the current turn.
const turnCache = new Map();
const ingestCache = new Map();
const degradedDeliveryCache = new Map();
let openClawRuntimeHealthPath = (
  process.env.DAEMON_CORE_OPENCLAW_HEALTH_FILE || ""
).trim();
let openClawRuntimeIncidentPath = openClawRuntimeHealthPath
  ? join(
    dirname(openClawRuntimeHealthPath),
    "openclaw-runtime-incidents.jsonl",
  )
  : "";
const OPENCLAW_HEALTH_SESSION_TTL_MS = 24 * 60 * 60 * 1000;
const OPENCLAW_HEALTH_MAX_SESSIONS = 64;
const TRANSCRIPT_RECONCILIATION_MAX_EVENTS = 1024;
const TRANSCRIPT_RECONCILIATION_MAX_TURNS = 64;
const TRANSCRIPT_RECONCILIATION_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const TRANSCRIPT_RECONCILIATION_COOLDOWN_MS = 60 * 1000;
const transcriptReconciliationCache = new Map();
const CODEX_DIRECT_JSON_TOOL_RESULT_NOTE = (
  " With the Codex engine, the call returns a JSON string directly: read or " +
  "parse that value itself; do not look for a `.content` property."
);

function opaqueRuntimeRef(value) {
  return createHash("sha256")
    .update(String(value || "unknown"))
    .digest("hex")
    .slice(0, 16);
}

function runtimeHealthState() {
  try {
    if (!existsSync(openClawRuntimeHealthPath)) return {};
    const parsed = JSON.parse(
      readFileSync(openClawRuntimeHealthPath, "utf8"),
    );
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function writeOpenClawRuntimeHealth({ sessionId, status, reason = "" }) {
  if (!openClawRuntimeHealthPath) return;
  try {
    const now = Date.now();
    const previous = runtimeHealthState();
    const sessions = (
      previous.sessions && typeof previous.sessions === "object"
        ? { ...previous.sessions }
        : {}
    );
    for (const [key, value] of Object.entries(sessions)) {
      if (
        !value ||
        typeof value.updated_at_ms !== "number" ||
        now - value.updated_at_ms > OPENCLAW_HEALTH_SESSION_TTL_MS
      ) {
        delete sessions[key];
      }
    }
    const sessionRef = opaqueRuntimeRef(sessionId);
    sessions[sessionRef] = {
      status,
      reason,
      updated_at_ms: now,
    };
    const ordered = Object.entries(sessions).sort(
      (left, right) => right[1].updated_at_ms - left[1].updated_at_ms,
    );
    const bounded = Object.fromEntries(
      ordered.slice(0, OPENCLAW_HEALTH_MAX_SESSIONS),
    );
    const degraded = Object.values(bounded).some(
      (value) => value.status === "degraded",
    );
    const payload = {
      schema: "daemon.openclaw-runtime-health.v1",
      status: degraded ? "degraded" : "healthy",
      updated_at_ms: now,
      sessions: bounded,
    };
    mkdirSync(dirname(openClawRuntimeHealthPath), {
      recursive: true,
      mode: 0o700,
    });
    const temporary = `${openClawRuntimeHealthPath}.${process.pid}.${randomUUID()}.tmp`;
    writeFileSync(temporary, `${JSON.stringify(payload)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    renameSync(temporary, openClawRuntimeHealthPath);
  } catch {
    // Observability must never break a user turn.
  }
}

function recordOpenClawRuntimeIncident({ advancementKey, outcome, reason }) {
  if (!openClawRuntimeIncidentPath) return;
  try {
    mkdirSync(dirname(openClawRuntimeIncidentPath), {
      recursive: true,
      mode: 0o700,
    });
    appendFileSync(
      openClawRuntimeIncidentPath,
      `${JSON.stringify({
        schema: "daemon.openclaw-runtime-incident.v1",
        timestamp: new Date().toISOString(),
        advancement_ref: opaqueRuntimeRef(advancementKey),
        outcome,
        reason,
      })}\n`,
      { encoding: "utf8", mode: 0o600 },
    );
  } catch {
    // Internal logging must never break a user turn.
  }
}

function writeHostCapabilityEvent(event, payload = {}) {
  const tracePath = (process.env.HOST_CAPABILITY_TRACE_FILE || "").trim();
  if (!tracePath) return;
  const record = {
    schema: "daemon.openclaw-host-trace.v1",
    event,
    timestamp: new Date().toISOString(),
    epoch: Date.now() / 1000,
    campaign_id: process.env.HOST_CAPABILITY_CAMPAIGN_ID || "",
    run_id: process.env.HOST_CAPABILITY_RUN_ID || "",
    arm: process.env.HOST_CAPABILITY_ARM || "",
    case_id: process.env.HOST_CAPABILITY_CASE_ID || "",
    ...payload,
  };
  try {
    mkdirSync(dirname(tracePath), { recursive: true });
    appendFileSync(tracePath, `${JSON.stringify(record)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
  } catch {
    // Evaluation telemetry must never affect the host turn.
  }
}

function compactTimings(timings) {
  if (!timings || typeof timings !== "object") return "unavailable";
  return Object.entries(timings)
    .map(([phase, duration]) => `${phase}:${duration}`)
    .join(",");
}

function stripMarkdownSection(source, headings, level) {
  const text = String(source || "");
  if (!text) return "";
  const expected = new Set(headings);
  const boundary = new RegExp(`^#{1,${level}}(?!#)\\s+`);
  const kept = [];
  let skipping = false;
  for (const line of text.split("\n")) {
    const normalized = line.trim();
    if (!skipping && expected.has(normalized)) {
      skipping = true;
      continue;
    }
    if (skipping) {
      if (!boundary.test(normalized)) continue;
      skipping = false;
    }
    kept.push(line);
  }
  return kept.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

function projectSovereignPromptForOpenClaw(source) {
  // OpenClaw owns external-tool choice, ordering, retries and evidence
  // navigation, as well as the immediate plan for the current turn. Its native
  // prompt already defines that loop. The canonical voice still supplies
  // identity, exact memory and the live situation, but must not add a second
  // planner or tool orchestrator at the adapter boundary.
  const withoutHostTools = stripMarkdownSection(
    source,
    ["# Outils de l'hote", "# Outils de l'hôte"],
    1,
  );
  const withoutTurnThreshold = stripMarkdownSection(
    withoutHostTools,
    ["# Seuil du tour"],
    6,
  );
  return stripMarkdownSection(
    withoutTurnThreshold,
    [
      "## Frontiere des connaissances externes",
      "## Frontière des connaissances externes",
    ],
    2,
  );
}

function withOpenClawProgressPreamble(source) {
  const prompt = String(source || "").trim();
  if (!prompt) return prompt;
  if (prompt.includes(OPENCLAW_PROGRESS_PREAMBLE_CONTRACT)) return prompt;
  return `${prompt}\n\n${OPENCLAW_PROGRESS_PREAMBLE_CONTRACT}`;
}

function isTransientOpenClawToolFailure(content) {
  return (
    typeof content === "string" &&
    OPENCLAW_TRANSIENT_TOOL_FAILURE_RE.test(content.trim())
  );
}

function isOpenClawProgressSummary(content) {
  return (
    typeof content === "string" &&
    OPENCLAW_PROGRESS_SUMMARY_RE.test(content.trim())
  );
}

function positiveMessageId(value) {
  if (typeof value === "number") {
    return Number.isSafeInteger(value) && value > 0 ? value : null;
  }
  const normalized = String(value ?? "").trim();
  const match = normalized.match(/(?:^|:)(\d+)$/);
  const parsed = match ? Number.parseInt(match[1], 10) : Number.NaN;
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function eventMessageId(event, context) {
  for (const candidate of [
    event?.messageId,
    context?.messageId,
    event?.metadata?.messageId,
  ]) {
    const messageId = positiveMessageId(candidate);
    if (messageId !== null) return messageId;
  }
  return null;
}

function telegramDirectConversation(event, context) {
  const channel = String(
    context?.channelId ||
      event?.channel ||
      event?.metadata?.channel ||
      "",
  ).toLowerCase();
  if (channel !== "telegram") return null;
  const candidates = [
    context?.conversationId,
    event?.conversationId,
    event?.to,
    event?.from,
    event?.metadata?.to,
    context?.sessionKey,
    event?.sessionKey,
  ];
  let chatId = "";
  for (const candidate of candidates) {
    const normalized = String(candidate ?? "").trim();
    const direct = normalized.match(/^(?:telegram:)?(\d+)$/i);
    const session = normalized.match(
      /^agent:[^:]+:telegram:direct:(\d+)$/i,
    );
    const match = direct || session;
    if (match) {
      chatId = match[1];
      break;
    }
  }
  if (!chatId) return null;
  const accountId = String(
    context?.accountId || event?.accountId || event?.metadata?.accountId || "default",
  );
  return {
    key: `${accountId}:${chatId}`,
    accountId,
    chatId,
  };
}

function createTelegramProgressAnchorStore({
  rootDir = TELEGRAM_PROGRESS_ANCHOR_DIR,
  now = () => Date.now(),
} = {}) {
  const conversationDir = ({ accountId, chatId }) => join(
    rootDir,
    Buffer.from(String(accountId), "utf8").toString("base64url"),
    String(chatId),
  );
  const removeQuietly = (path) => {
    try {
      unlinkSync(path);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  };

  const note = (entry) => {
    const directory = conversationDir(entry);
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const target = join(directory, `${entry.messageId}.json`);
    const temporary = `${target}.${randomUUID()}.tmp`;
    try {
      writeFileSync(
        temporary,
        JSON.stringify({
          accountId: entry.accountId,
          chatId: entry.chatId,
          messageId: entry.messageId,
          at: entry.at,
        }),
        { encoding: "utf8", mode: 0o600 },
      );
      renameSync(temporary, target);
    } finally {
      removeQuietly(temporary);
    }
  };

  const takeBefore = (reference, messageId) => {
    const directory = conversationDir(reference);
    let names;
    try {
      names = readdirSync(directory);
    } catch (error) {
      if (error?.code === "ENOENT") return [];
      throw error;
    }
    const cutoff = now() - TELEGRAM_PROGRESS_STATE_TTL_MS;
    const anchors = [];
    for (const name of names) {
      if (!/^\d+\.json$/.test(name)) continue;
      const path = join(directory, name);
      let entry;
      try {
        entry = JSON.parse(readFileSync(path, "utf8"));
      } catch {
        removeQuietly(path);
        continue;
      }
      const anchorId = positiveMessageId(entry?.messageId);
      const timestamp = Number(entry?.at);
      const matches = (
        String(entry?.accountId) === reference.accountId &&
        String(entry?.chatId) === reference.chatId
      );
      if (
        !matches ||
        anchorId === null ||
        !Number.isFinite(timestamp) ||
        timestamp < cutoff
      ) {
        removeQuietly(path);
        continue;
      }
      if (anchorId >= messageId) continue;
      anchors.push({ messageId: anchorId, at: timestamp });
      removeQuietly(path);
    }
    return anchors;
  };

  return {
    note,
    takeBefore,
    clear() {
      rmSync(rootDir, { recursive: true, force: true });
    },
  };
}

async function deleteTelegramMessageViaBotApi({
  config,
  accountId,
  chatId,
  messageId,
  fetchImpl = globalThis.fetch,
  resolveAccount,
  timeoutMs = TELEGRAM_PROGRESS_DELETE_TIMEOUT_MS,
}) {
  let accountResolver = resolveAccount;
  if (typeof accountResolver !== "function") {
    const telegramAccount = await import(
      "openclaw/plugin-sdk/telegram-account"
    );
    accountResolver = telegramAccount.resolveTelegramAccount;
  }
  const account = accountResolver({
    cfg: config,
    accountId,
  });
  const token = String(account?.token || "").trim();
  if (!token) {
    throw new Error(`Telegram account ${accountId} has no bot token`);
  }
  if (typeof fetchImpl !== "function") {
    throw new Error("Telegram deleteMessage transport unavailable");
  }

  let response;
  try {
    response = await fetchImpl(
      `https://api.telegram.org/bot${token}/deleteMessage`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          chat_id: chatId,
          message_id: messageId,
        }),
        signal: AbortSignal.timeout(timeoutMs),
      },
    );
  } catch (error) {
    const reason = error instanceof Error ? error.name : "transport_error";
    throw new Error(`Telegram deleteMessage request failed (${reason})`);
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // The status below remains enough for a safe, token-free diagnostic.
  }
  if (!response.ok || payload?.ok !== true) {
    const status = Number(payload?.error_code) || response.status || "unknown";
    throw new Error(`Telegram deleteMessage failed (${status})`);
  }
}

function createTelegramProgressCleaner({
  enabled,
  cleanupDelayMs = TELEGRAM_PROGRESS_CLEANUP_DELAY_MS,
  now = () => Date.now(),
  sleep = (durationMs) => new Promise((resolve) => setTimeout(resolve, durationMs)),
  deleteMessage,
  anchorStore,
  onDecision = () => {},
}) {
  const conversations = new Map();

  const stateFor = (reference, { allowUniqueChatFallback = false } = {}) => {
    const timestamp = now();
    for (const [key, state] of conversations) {
      if (timestamp - state.updatedAt > TELEGRAM_PROGRESS_STATE_TTL_MS) {
        conversations.delete(key);
      }
    }
    let state = conversations.get(reference.key);
    if (!state && allowUniqueChatFallback) {
      const chatMatches = [...conversations.entries()].filter(
        ([, candidate]) => candidate.chatId === reference.chatId,
      );
      if (chatMatches.length === 1) {
        const [previousKey, candidate] = chatMatches[0];
        conversations.delete(previousKey);
        state = candidate;
        state.accountId = reference.accountId;
        conversations.set(reference.key, state);
      }
    }
    if (!state) {
      state = {
        accountId: reference.accountId,
        chatId: reference.chatId,
        observed: new Map(),
        pendingInbound: new Map(),
        pendingSends: [],
        updatedAt: timestamp,
      };
      conversations.set(reference.key, state);
    }
    state.updatedAt = timestamp;
    return state;
  };

  const pruneState = (state) => {
    const cutoff = now() - TELEGRAM_PROGRESS_STATE_TTL_MS;
    for (const [messageId, entry] of state.observed) {
      if (entry.at < cutoff) state.observed.delete(messageId);
    }
    for (const [messageId, receivedAt] of state.pendingInbound) {
      if (receivedAt < cutoff) state.pendingInbound.delete(messageId);
    }
    state.pendingSends = state.pendingSends.filter((entry) => entry.at >= cutoff);
  };

  const noteReceived = (event, context) => {
    if (!enabled) return null;
    const reference = telegramDirectConversation(event, context);
    const messageId = eventMessageId(event, context);
    if (!reference || messageId === null) {
      onDecision({
        status: "inbound_unobservable",
        hasReference: Boolean(reference),
        hasMessageId: messageId !== null,
      });
      return null;
    }
    const state = stateFor(reference);
    pruneState(state);
    const timestamp = now();
    state.observed.set(messageId, { direction: "inbound", at: timestamp });
    state.pendingInbound.set(messageId, timestamp);
    if (anchorStore) {
      try {
        anchorStore.note({ ...reference, messageId, at: timestamp });
      } catch (error) {
        onDecision({
          status: "anchor_store_failed",
          ...reference,
          messageId,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
    return { ...reference, messageId };
  };

  const noteDispatch = (event) => {
    if (!enabled) return null;
    const source = event?.ctx;
    const channelId = String(
      source?.OriginatingChannel || source?.Surface || source?.Provider || "",
    ).toLowerCase();
    if (channelId !== "telegram") return null;
    return noteReceived(
      {
        messageId: (
          source?.MessageSidFull ||
          source?.MessageSid ||
          source?.MessageSidFirst ||
          source?.MessageSidLast
        ),
        from: source?.From,
        accountId: source?.AccountId,
        sessionKey: event?.sessionKey || source?.SessionKey,
      },
      {
        channelId,
        accountId: source?.AccountId,
        conversationId: (
          source?.OriginatingTo || source?.To || source?.From
        ),
        sessionKey: event?.sessionKey || source?.SessionKey,
      },
    );
  };

  const noteSending = (content, event, context) => {
    if (!enabled || !content) return;
    const reference = telegramDirectConversation(event, context);
    if (!reference) return;
    const state = stateFor(reference, { allowUniqueChatFallback: true });
    pruneState(state);
    const timestamp = now();
    const replyToId = positiveMessageId(event?.replyToId ?? context?.replyToId);
    if (replyToId !== null) {
      state.observed.set(replyToId, { direction: "inbound", at: timestamp });
      state.pendingInbound.set(replyToId, timestamp);
    }
    state.pendingSends.push({ content: content.trim(), at: timestamp });
  };

  const noteSent = async (event, context) => {
    if (!enabled || event?.success !== true) return;
    const reference = telegramDirectConversation(event, context);
    const messageId = eventMessageId(event, context);
    if (!reference || messageId === null) {
      onDecision({
        status: "outbound_unobservable",
        hasReference: Boolean(reference),
        hasMessageId: messageId !== null,
      });
      return;
    }
    const state = stateFor(reference);
    pruneState(state);
    const deliveredContent = String(event?.content || "").trim();
    const pendingSendIndex = state.pendingSends.findIndex(
      (entry) => entry.content === deliveredContent,
    );
    state.observed.set(messageId, { direction: "outbound", at: now() });
    if (pendingSendIndex < 0) {
      onDecision({ status: "unmatched_final", ...reference, messageId });
      return;
    }
    state.pendingSends.splice(pendingSendIndex, 1);
    if (anchorStore) {
      try {
        for (const anchor of anchorStore.takeBefore(reference, messageId)) {
          state.observed.set(anchor.messageId, {
            direction: "inbound",
            at: anchor.at,
          });
          state.pendingInbound.set(anchor.messageId, anchor.at);
        }
      } catch (error) {
        onDecision({
          status: "anchor_store_failed",
          ...reference,
          messageId,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }

    const anchors = [...state.pendingInbound.keys()]
      .filter((candidate) => candidate < messageId)
      .sort((left, right) => left - right);
    if (anchors.length === 0) {
      onDecision({ status: "missing_anchor", ...reference, messageId });
      return;
    }
    const anchor = anchors[0];
    for (const candidate of anchors) state.pendingInbound.delete(candidate);

    const span = messageId - anchor - 1;
    if (span <= 0) {
      onDecision({ status: "no_progress", ...reference, messageId });
      return;
    }
    if (span > TELEGRAM_PROGRESS_MAX_ID_SPAN) {
      onDecision({ status: "ambiguous", ...reference, messageId, span });
      return;
    }
    const unknownIds = [];
    for (let candidate = anchor + 1; candidate < messageId; candidate += 1) {
      if (!state.observed.has(candidate)) unknownIds.push(candidate);
    }
    if (unknownIds.length !== 1) {
      onDecision({
        status: "ambiguous",
        ...reference,
        messageId,
        unknownCount: unknownIds.length,
      });
      return;
    }

    const progressMessageId = unknownIds[0];
    onDecision({
      status: "scheduled",
      ...reference,
      messageId,
      progressMessageId,
    });
    await sleep(cleanupDelayMs);
    try {
      await deleteMessage({ ...reference, messageId: progressMessageId });
      onDecision({
        status: "deleted",
        ...reference,
        messageId,
        progressMessageId,
      });
    } catch (error) {
      onDecision({
        status: "failed",
        ...reference,
        messageId,
        progressMessageId,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  };

  return {
    noteDispatch,
    noteReceived,
    noteSending,
    noteSent,
    clear() {
      conversations.clear();
      anchorStore?.clear();
    },
  };
}

function pruneTurnCache() {
  const cutoff = Date.now() - TURN_CACHE_TTL_MS;
  for (const [key, value] of turnCache) {
    if (!value || value.updatedAt < cutoff) turnCache.delete(key);
  }
  while (turnCache.size > TURN_CACHE_MAX_ENTRIES) {
    turnCache.delete(turnCache.keys().next().value);
  }
  for (const [key, value] of ingestCache) {
    if (!value || value.updatedAt < cutoff) ingestCache.delete(key);
  }
  while (ingestCache.size > TURN_CACHE_MAX_ENTRIES * 4) {
    ingestCache.delete(ingestCache.keys().next().value);
  }
  for (const [key, value] of degradedDeliveryCache) {
    if (!value || value.updatedAt < cutoff) degradedDeliveryCache.delete(key);
  }
}

function turnState(sessionId) {
  if (!sessionId) return null;
  pruneTurnCache();
  const state = turnCache.get(sessionId) || {
    lastUserMessage: "",
    selectedSouvenirs: [],
    route: {},
    sovereignSystemPrompt: "",
    sovereignDeveloperBackground: "",
    sovereignNearTurnPrompt: "",
    structuredPromptTiers: false,
    memoryToolsActive: false,
    voicePath: "",
    degraded: false,
    degradationReason: "",
    canaryTraceId: randomUUID().replaceAll("-", "").slice(0, 16),
    sourceTurnStartedAt: 0,
    sceneScope: "",
    userL0Ref: {},
    readSouvenirs: [],
    libraryPreflightCalls: 0,
    lastMemoryToolResult: null,
    technicalHeartbeat: false,
    assembleStartedAt: 0,
    assembleCompletedAt: 0,
    promptAppliedAt: 0,
    updatedAt: Date.now(),
  };
  state.updatedAt = Date.now();
  turnCache.set(sessionId, state);
  return state;
}

function deliveryTurnState(event, context) {
  const sessionKey = (
    context?.sessionKey ||
    context?.sessionId ||
    event?.sessionKey ||
    event?.sessionId ||
    ""
  );
  return sessionKey
    ? turnCache.get(sessionKey) || degradedDeliveryCache.get(sessionKey) || null
    : null;
}

function jsonToolResult(value) {
  return {
    content: [{ type: "text", text: JSON.stringify(value) }],
    details: value,
  };
}

function stableJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableJson(item)).join(",")}]`;
  }
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${stableJson(value[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}

function memoryToolCallKey(name, args) {
  return `${name}\u0000${stableJson(args)}`;
}

function isReusableMemoryToolResult(result) {
  return ["capsule_sources", "found", "ok", "partial"].includes(
    String(result?.status || ""),
  );
}

function normalizeSouvenirReadArguments(args) {
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    return { arguments: args, repaired: false, conflict: false };
  }
  const rootNeed = typeof args.need === "string" ? args.need.trim() : "";
  if (!rootNeed) {
    return { arguments: args, repaired: false, conflict: false };
  }
  const scene = args.scene;
  if (!scene || typeof scene !== "object" || Array.isArray(scene)) {
    return { arguments: args, repaired: false, conflict: false };
  }
  const sceneNeed = typeof scene.need === "string" ? scene.need.trim() : "";
  if (sceneNeed && sceneNeed !== rootNeed) {
    return { arguments: args, repaired: false, conflict: true };
  }
  const normalized = {
    ...args,
    scene: { ...scene, need: sceneNeed || rootNeed },
  };
  delete normalized.need;
  return { arguments: normalized, repaired: true, conflict: false };
}

function createMemoryTool({ name, description, parameters, daemonUrl, userId, context }) {
  return {
    name,
    label:
      name === SOUVENIR_READ_TOOL
        ? "Read Personal Memory"
        : "Recall Personal History",
    description,
    parameters,
    // OpenClaw Tool Search (on by default since 2026.10) would otherwise hide
    // memory tools behind tool_search; the voice relies on calling them directly.
    // Earlier hosts ignore the property.
    catalogMode: "direct-only",
    async execute(_toolCallId, args) {
      const sessionId = context?.sessionId || "";
      const state = sessionId ? turnCache.get(sessionId) : null;
      const startedAt = Date.now();
      writeHostCapabilityEvent("tool_started", { tool_name: name });
      if (!state?.memoryToolsActive) {
        writeHostCapabilityEvent("tool_completed", {
          tool_name: name,
          status: "unavailable",
          duration_ms: Date.now() - startedAt,
        });
        return jsonToolResult({
          status: "unavailable",
          reason: "The sovereign library is not active for this turn.",
        });
      }
      const normalization = name === SOUVENIR_READ_TOOL
        ? normalizeSouvenirReadArguments(args)
        : { arguments: args, repaired: false, conflict: false };
      if (normalization.conflict) {
        writeHostCapabilityEvent("tool_completed", {
          tool_name: name,
          status: "error",
          duration_ms: Date.now() - startedAt,
        });
        return jsonToolResult({
          status: "error",
          reason: "The requested evidence is inconsistent within the tool call.",
        });
      }
      const callKey = memoryToolCallKey(name, normalization.arguments);
      if (
        state.lastMemoryToolResult?.key === callKey &&
        isReusableMemoryToolResult(state.lastMemoryToolResult.result)
      ) {
        writeHostCapabilityEvent("tool_completed", {
          tool_name: name,
          status: "replayed_exact_result",
          duration_ms: Date.now() - startedAt,
        });
        return jsonToolResult(state.lastMemoryToolResult.result);
      }
      // Another memory call breaks immediate repetition. The previous result
      // only makes the same call's tool-to-model handoff reliable;
      // it never becomes a second memory store in the adapter.
      state.lastMemoryToolResult = null;
      try {
        const data = await postJson(`${daemonUrl}/openclaw/tool`, {
          user_id: userId,
          session_id: sessionId,
          trace_id: state.canaryTraceId || "",
          name,
          arguments: normalization.arguments && typeof normalization.arguments === "object"
            ? normalization.arguments
            : {},
          read_souvenirs: state.readSouvenirs,
          library_preflight_calls: state.libraryPreflightCalls,
        });
        state.readSouvenirs = Array.isArray(data.read_souvenirs)
          ? data.read_souvenirs
          : state.readSouvenirs;
        if (data.library_preflight_consumed) state.libraryPreflightCalls += 1;
        state.updatedAt = Date.now();
        const result = data.result || {
          status: "error",
          reason: "Empty response from the daemon.",
        };
        if (isReusableMemoryToolResult(result)) {
          state.lastMemoryToolResult = { key: callKey, result };
        }
        writeHostCapabilityEvent("tool_completed", {
          tool_name: name,
          status: result.status || "unknown",
          duration_ms: Date.now() - startedAt,
        });
        return jsonToolResult(result);
      } catch (err) {
        writeHostCapabilityEvent("tool_completed", {
          tool_name: name,
          status: "error",
          duration_ms: Date.now() - startedAt,
        });
        return jsonToolResult({
          status: "error",
          reason: `Daemon tool unavailable (${err.message}). Infer nothing from this.`,
        });
      }
    },
  };
}

async function postJson(url, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

function messageText(message) {
  if (typeof message?.content === "string") return message.content;
  if (!Array.isArray(message?.content)) return "";
  return message.content
    .map((part) => (typeof part === "string" ? part : part?.text ?? ""))
    .join(" ")
    .trim();
}

function messageOccurredAt(message) {
  const raw = message?.timestamp;
  if (typeof raw === "number" && Number.isFinite(raw) && raw > 0) {
    return raw > 10_000_000_000 ? raw / 1000 : raw;
  }
  if (typeof raw === "string" && raw.trim()) {
    const numeric = Number(raw);
    if (Number.isFinite(numeric) && numeric > 0) {
      return numeric > 10_000_000_000 ? numeric / 1000 : numeric;
    }
    const parsed = Date.parse(raw);
    if (Number.isFinite(parsed)) return parsed / 1000;
  }
  return 0;
}

function normalizedDurableMessages(messages) {
  return (Array.isArray(messages) ? messages : [])
    .filter((message) => (
      message?.role === "user" || message?.role === "assistant"
    ))
    .map((message) => ({
      role: message.role,
      content: message.role === "assistant"
        ? stripDegradedResponseNotice(
          stripInternalTurnMetadata(messageText(message)),
        )
        : messageText(message),
      occurredAt: messageOccurredAt(message),
    }))
    .filter((message) => message.content);
}

async function readAcceptedTurnFromOpenClawTranscript(admission) {
  const storePath = String(admission?.storePath || "").trim();
  const sessionId = String(admission?.sessionId || "").trim();
  const entryId = String(admission?.entryId || "").trim();
  const activeMessagePosition = Number(admission?.activeMessagePosition);
  if (
    !storePath ||
    !sessionId ||
    !entryId ||
    !Number.isSafeInteger(activeMessagePosition) ||
    activeMessagePosition < 0
  ) {
    return { kind: "unavailable", messages: [] };
  }

  let database;
  try {
    const { DatabaseSync } = await import("node:sqlite");
    database = new DatabaseSync(storePath, { readOnly: true });
    const rows = database.prepare(`
      SELECT transcript.event_json AS event_json
      FROM session_transcript_active_events AS active
      JOIN transcript_events AS transcript
        ON transcript.session_id = active.session_id
       AND transcript.seq = active.event_seq
      WHERE active.session_id = ?
        AND active.message_position IS NOT NULL
        AND active.message_position >= ?
      ORDER BY active.message_position ASC
      LIMIT 512
    `).all(sessionId, activeMessagePosition);

    const recovered = [];
    let admitted = false;
    let laterUser = false;
    for (const row of rows) {
      let event;
      try {
        event = JSON.parse(row.event_json);
      } catch {
        continue;
      }
      if (event?.type !== "message") continue;
      const message = (
        event?.message && typeof event.message === "object"
          ? event.message
          : event
      );
      if (!admitted) {
        if (event?.id !== entryId || message?.role !== "user") continue;
        admitted = true;
        recovered.push(message);
        continue;
      }
      if (message?.role === "user") {
        laterUser = true;
        break;
      }
      recovered.push(message);
    }

    const durable = normalizedDurableMessages(recovered);
    if (
      durable.some((message) => message.role === "user") &&
      durable.some((message) => message.role === "assistant")
    ) {
      return { kind: "recovered", messages: recovered };
    }
    return {
      kind: laterUser ? "closed_without_assistant" : "pending",
      messages: recovered,
    };
  } catch {
    return { kind: "unavailable", messages: [] };
  } finally {
    try {
      database?.close();
    } catch {
      // Read-only: closing must not hide the original diagnostic.
    }
  }
}

async function readRecentClosedTurnsFromOpenClawTranscript(admission) {
  const storePath = String(admission?.storePath || "").trim();
  const sessionId = String(admission?.sessionId || "").trim();
  if (!storePath || !sessionId) return [];

  let database;
  try {
    const { DatabaseSync } = await import("node:sqlite");
    database = new DatabaseSync(storePath, { readOnly: true });
    const rows = database.prepare(`
      SELECT event_json
      FROM (
        SELECT transcript.event_json AS event_json,
               active.message_position AS message_position
        FROM session_transcript_active_events AS active
        JOIN transcript_events AS transcript
          ON transcript.session_id = active.session_id
         AND transcript.seq = active.event_seq
        WHERE active.session_id = ?
          AND active.message_position IS NOT NULL
        ORDER BY active.message_position DESC
        LIMIT ?
      )
      ORDER BY message_position ASC
    `).all(sessionId, TRANSCRIPT_RECONCILIATION_MAX_EVENTS);

    const turns = [];
    let current = null;
    const closeCurrent = () => {
      if (current?.user && current?.assistant) turns.push(current);
      current = null;
    };
    for (const row of rows) {
      let event;
      try {
        event = JSON.parse(row.event_json);
      } catch {
        continue;
      }
      if (event?.type !== "message") continue;
      const message = (
        event?.message && typeof event.message === "object"
          ? event.message
          : event
      );
      if (message?.role === "user") {
        closeCurrent();
        const content = messageText(message);
        const occurredAt = messageOccurredAt(message) || messageOccurredAt(event);
        current = (
          content && occurredAt > 0 && event?.id
            ? {
                user: {
                  event_id: String(event.id),
                  role: "user",
                  content,
                  occurred_at: occurredAt,
                },
                assistant: null,
              }
            : null
        );
        continue;
      }
      if (
        !current ||
        message?.role !== "assistant" ||
        message?.__openclaw?.runTerminal !== true
      ) {
        continue;
      }
      const content = stripDegradedResponseNotice(
        stripInternalTurnMetadata(messageText(message)),
      );
      const occurredAt = messageOccurredAt(message) || messageOccurredAt(event);
      if (content && occurredAt > 0 && event?.id) {
        current.assistant = {
          event_id: String(event.id),
          role: "assistant",
          content,
          occurred_at: occurredAt,
        };
      }
    }
    closeCurrent();

    const cutoff = Date.now() - TRANSCRIPT_RECONCILIATION_MAX_AGE_MS;
    return turns
      .filter((turn) => turn.user.occurred_at * 1000 >= cutoff)
      .slice(-TRANSCRIPT_RECONCILIATION_MAX_TURNS)
      .flatMap((turn) => [turn.user, turn.assistant]);
  } catch {
    return [];
  } finally {
    try {
      database?.close();
    } catch {
      // Read-only: closing must not affect the host turn.
    }
  }
}

function scheduleRecentTranscriptReconciliation({
  admission,
  daemonUrl,
  userId,
  log,
}) {
  const storePath = String(admission?.storePath || "").trim();
  const sessionId = String(admission?.sessionId || "").trim();
  if (!storePath || !sessionId) return;
  const cacheKey = `${storePath}\u0000${sessionId}`;
  const now = Date.now();
  const previous = transcriptReconciliationCache.get(cacheKey);
  if (
    previous?.inflight ||
    (previous?.attemptedAt &&
      now - previous.attemptedAt < TRANSCRIPT_RECONCILIATION_COOLDOWN_MS)
  ) {
    return;
  }
  transcriptReconciliationCache.set(cacheKey, {
    inflight: true,
    attemptedAt: now,
  });
  void (async () => {
    const messages = await readRecentClosedTurnsFromOpenClawTranscript(admission);
    if (messages.length === 0) {
      transcriptReconciliationCache.set(cacheKey, {
        inflight: false,
        attemptedAt: now,
      });
      return;
    }
    const result = await postJson(
      `${daemonUrl}/openclaw/reconcile-transcript`,
      {
        user_id: userId,
        session_id: sessionId,
        messages,
      },
    );
    transcriptReconciliationCache.set(cacheKey, {
      inflight: false,
      attemptedAt: now,
    });
    log.info?.(
      `[daemon-harness] transcript reconciliation completed` +
        ` | session=${sessionId}` +
        ` | scanned=${Number(result?.scanned) || 0}` +
        ` | existing=${Number(result?.already_recorded) || 0}` +
        ` | restored=${Number(result?.restored) || 0}`,
    );
  })().catch((error) => {
    transcriptReconciliationCache.set(cacheKey, {
      inflight: false,
      attemptedAt: now,
    });
    log.warn?.(
      `[daemon-harness] transcript reconciliation deferred: ${error.message}`,
    );
  });
}

function canonicalDialogueKey(message) {
  if (message?.role !== "user" && message?.role !== "assistant") return null;
  const content = messageText(message)
    .replace(INTERNAL_TURN_TIME_LINE_RE, "")
    .replaceAll("\r\n", "\n")
    .trim();
  return content ? `${message.role}\u0000${content}` : null;
}

function mergeCanonicalRecentDialogue(messages, canonicalDialogue) {
  const hostMessages = Array.isArray(messages) ? messages : [];
  if (!Array.isArray(canonicalDialogue) || canonicalDialogue.length === 0) {
    return { messages: hostMessages, missing: [], added: 0, canonicalCount: 0 };
  }

  const canonical = canonicalDialogue.filter((message) => (
    canonicalDialogueKey(message) !== null && typeof message?.content === "string"
  ));
  if (canonical.length === 0) {
    return { messages: hostMessages, missing: [], added: 0, canonicalCount: 0 };
  }

  const canonicalKeys = canonical.map(canonicalDialogueKey);
  const hostKeys = hostMessages
    .map(canonicalDialogueKey)
    .filter((key) => key !== null);
  const rows = canonicalKeys.length + 1;
  const columns = hostKeys.length + 1;
  const lcs = Array.from({ length: rows }, () => new Uint16Array(columns));
  for (let i = canonicalKeys.length - 1; i >= 0; i -= 1) {
    for (let j = hostKeys.length - 1; j >= 0; j -= 1) {
      lcs[i][j] = canonicalKeys[i] === hostKeys[j]
        ? lcs[i + 1][j + 1] + 1
        : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }

  const matchedCanonical = new Set();
  let i = 0;
  let j = 0;
  while (i < canonicalKeys.length && j < hostKeys.length) {
    if (canonicalKeys[i] === hostKeys[j]) {
      matchedCanonical.add(i);
      i += 1;
      j += 1;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      i += 1;
    } else {
      j += 1;
    }
  }

  const missing = canonical.filter((_, index) => !matchedCanonical.has(index));
  return {
    messages: missing.length > 0 ? [...missing, ...hostMessages] : hostMessages,
    missing,
    added: missing.length,
    canonicalCount: canonical.length,
  };
}

function codexTurnProjection(state, messages) {
  const epochSeed = state?.canaryTraceId || randomUUID().replaceAll("-", "");
  const fingerprint = createHash("sha256")
    .update(CODEX_TURN_PROJECTION_VERSION)
    .update("\u0000")
    .update(stableJson(messages))
    .update("\u0000")
    .update(String(state?.sovereignSystemPrompt || ""))
    .update("\u0000")
    .update(String(state?.sovereignDeveloperBackground || ""))
    .update("\u0000")
    .update(String(state?.sovereignNearTurnPrompt || ""))
    .digest("hex")
    .slice(0, 16);
  return {
    mode: "thread_bootstrap",
    epoch: `daemon-turn:${epochSeed}`,
    fingerprint,
  };
}

function projectsIntoPersistentCodexThread(runtimeSettings) {
  return runtimeSettings?.executionHost?.id === "codex-app-server";
}

function lastUserText(messages) {
  if (!Array.isArray(messages)) return "";
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m && m.role === "user") return messageText(m);
  }
  return "";
}

function retrievalMessageFromPrompt(prompt) {
  const incoming = typeof prompt === "string" ? prompt.trim() : "";
  if (!incoming) return "";
  const requestBoundary = incoming.indexOf(
    OPENCLAW_CURRENT_USER_REQUEST_MARKER,
  );
  if (requestBoundary < 0) return incoming;
  const envelope = incoming.slice(0, requestBoundary);
  if (
    !envelope.includes(OPENCLAW_CONVERSATION_INFO_MARKER) &&
    !envelope.includes(OPENCLAW_ASSEMBLED_CONTEXT_MARKER)
  ) {
    return incoming;
  }
  const currentRequest = incoming
    .slice(requestBoundary + OPENCLAW_CURRENT_USER_REQUEST_MARKER.length)
    .trim();
  return currentRequest || incoming;
}

function retrievalUserMessage({ prompt, messages, state }) {
  const ingested = typeof state?.lastUserMessage === "string"
    ? state.lastUserMessage.trim()
    : "";
  if (ingested) return retrievalMessageFromPrompt(ingested);
  return retrievalMessageFromPrompt(prompt) || lastUserText(messages).trim();
}

function isTechnicalHeartbeat(prompt, messages) {
  const incoming = typeof prompt === "string" ? prompt.trim() : "";
  if (incoming === OPENCLAW_HEARTBEAT_POLL) return true;
  return (
    lastUserText(messages).trim() === OPENCLAW_HEARTBEAT_POLL &&
    incoming.startsWith(OPENCLAW_HEARTBEAT_PROMPT_PREFIX) &&
    incoming.includes("reply HEARTBEAT_OK")
  );
}

function stripInternalTurnMetadata(text) {
  if (typeof text !== "string" || !text) return text;
  const withoutTime = text.replace(INTERNAL_TURN_TIME_LINE_RE, "");
  const commitments = [];
  let cleaned = withoutTime;
  while (true) {
    const line = cleaned.match(INTERNAL_TURN_COMMITMENT_LINE_RE);
    if (!line) break;
    const parsed = line[0].match(INTERNAL_TURN_COMMITMENT_RE);
    if (parsed) commitments.push(parsed);
    cleaned = cleaned.slice(line[0].length);
  }
  const commitment = (
    commitments.find((item) => item[2].trim()) || commitments[0]
  );
  if (
    commitment &&
    commitment[1].toLowerCase() === "clarify" &&
    commitment[2].trim()
  ) {
    return commitment[2].trim();
  }
  const metadataRemoved = withoutTime !== text || cleaned !== withoutTime;
  const delivered = metadataRemoved
    ? cleaned.replace(/^[\r\n]+/, "")
    : text;
  if (
    commitment &&
    commitment[1].toLowerCase() === "condition" &&
    commitment[2].trim()
  ) {
    return joinCondition(commitment[2].trim(), delivered);
  }
  if (
    commitment &&
    ["exchange", "direct"].includes(commitment[1].toLowerCase()) &&
    commitment[2].trim()
  ) {
    return joinCondition(commitment[2].trim(), delivered);
  }
  return delivered;
}

function stripDegradedResponseNotice(text) {
  if (
    typeof text !== "string" ||
    !text.startsWith(DEGRADED_RESPONSE_NOTICE)
  ) {
    return text;
  }
  return text.slice(DEGRADED_RESPONSE_NOTICE.length).replace(/^[\r\n]+/, "");
}

function cleanVisibleAssistantText(text) {
  return stripDegradedResponseNotice(stripInternalTurnMetadata(text));
}

function cleanAssistantMessage(message) {
  if (!message || message.role !== "assistant") return message;
  if (typeof message.content === "string") {
    const content = cleanVisibleAssistantText(message.content);
    return content === message.content ? message : { ...message, content };
  }
  if (!Array.isArray(message.content)) return message;
  let changed = false;
  const content = message.content.map((part) => {
    if (typeof part === "string") {
      const cleaned = cleanVisibleAssistantText(part);
      if (cleaned !== part) changed = true;
      return cleaned;
    }
    if (!part || typeof part.text !== "string") return part;
    const text = cleanVisibleAssistantText(part.text);
    if (text === part.text) return part;
    changed = true;
    return { ...part, text };
  });
  return changed ? { ...message, content } : message;
}

function cleanCodexLlmOutput(event) {
  // Codex app-server does not yet run its output through plugin text
  // transforms. Its llm_output hook receives the same arrays and objects
  // that will later be delivered. Clean them synchronously here
  // to preserve native progress drafts without adding a delivery hook
  // that would disable those drafts.
  if (Array.isArray(event?.assistantTexts)) {
    for (let index = 0; index < event.assistantTexts.length; index += 1) {
      const text = event.assistantTexts[index];
      if (typeof text === "string") {
        event.assistantTexts[index] = cleanVisibleAssistantText(text);
      }
    }
  }
  const lastAssistant = event?.lastAssistant;
  const cleaned = cleanAssistantMessage(lastAssistant);
  if (cleaned !== lastAssistant && lastAssistant && cleaned) {
    lastAssistant.content = cleaned.content;
  }
}

function joinCondition(movement, delivered) {
  const body = delivered.replace(/^[\r\n]+/, "");
  if (!body) return movement;
  if (body.startsWith(movement)) return body;
  return `${movement}\n${body}`;
}

// Rough token estimate, used by OpenClaw's compaction threshold.
function estimateTokens(messages) {
  if (!Array.isArray(messages)) return 0;
  let chars = 0;
  for (const m of messages) {
    if (typeof m?.content === "string") chars += m.content.length;
    else if (Array.isArray(m?.content)) {
      for (const part of m.content) chars += (part?.text ?? "").length;
    }
  }
  return Math.ceil(chars / 4);
}

export default function register(api) {
  const registrationConfig = api.pluginConfig || {};
  openClawRuntimeHealthPath = String(
    registrationConfig.runtimeHealthPath ||
    process.env.DAEMON_CORE_OPENCLAW_HEALTH_FILE ||
    "",
  ).trim();
  openClawRuntimeIncidentPath = openClawRuntimeHealthPath
    ? join(
      dirname(openClawRuntimeHealthPath),
      "openclaw-runtime-incidents.jsonl",
    )
    : "";
  const toolDaemonUrl = (
    registrationConfig.daemonUrl ||
    process.env.DAEMON_HARNESS_URL ||
    "http://127.0.0.1:8000"
  ).replace(/\/$/, "");
  const toolUserId =
    registrationConfig.userId || process.env.DAEMON_CORE_USER_ID || "default";
  // Output transforms run inside the provider stream, before preview and final
  // delivery. Do not register an outbound modifying hook here: OpenClaw
  // deliberately disables Telegram provider previews when one is present.
  if (typeof api.registerTextTransforms === "function") {
    api.registerTextTransforms({
      output: OPENCLAW_OUTPUT_TEXT_TRANSFORMS,
    });
  }

  // Codex app-server persists and delivers responses outside the transforms
  // above. Clean its persisted copy and turn result before final delivery,
  // without registering message_sending, which would remove the
  // Telegram progress draft showing tool activity.
  api.on("before_message_write", (event) => {
    const message = cleanAssistantMessage(event?.message);
    return message === event?.message ? undefined : { message };
  });
  api.on("llm_output", (event) => {
    cleanCodexLlmOutput(event);
  });

  if (typeof api.registerTool === "function") {
    api.registerTool(
      (context) => createMemoryTool({
        name: SOUVENIR_READ_TOOL,
        description: (
          "Open the exact content of active personal souvenirs from exact paths selected in the catalog supplied to the voice. Reading is bounded to eight souvenirs; the catalog alone is never a source of facts." +
          CODEX_DIRECT_JSON_TOOL_RESULT_NOTE
        ),
        parameters: {
          type: "object",
          properties: {
            paths: {
              type: "array",
              items: { type: "string" },
              minItems: 1,
              maxItems: 8,
              description: (
                "One to eight canonical paths copied exactly from the catalog, without brackets or the .md extension."
              ),
            },
            collection_path: {
              type: "string",
              description: (
                "Exact path of a collection marked in the catalog, also present in paths. The same call opens a bounded page of its proven members."
              ),
            },
            collection_offset: {
              type: "integer",
              minimum: 0,
              default: 0,
              description: (
                "Explicit offset for a subsequent page announced by the previous read."
              ),
            },
            scene: {
              type: "object",
              description: (
                "Minimal assessment of the whole situation before reading, drawn from the message and the entire scene rather than just the selected paths."
              ),
              properties: {
                established: {
                  type: "array",
                  items: { type: "string" },
                  maxItems: 8,
                  description: (
                    "People, place, time, constraints or other established conditions that may change the response. Include the current social and physical setting, not only the subjects of the latest sentence. A still-unknown property neither downgrades nor removes an already-established condition."
                  ),
                },
                uncertain: {
                  type: "array",
                  items: { type: "string" },
                  maxItems: 8,
                  description: (
                    "Contingent unknowns in the current scene that would materially change the response, one per entry and only if the response must expose them at the requested level of precision. Do not include only the durable preferences the read must retrieve. Name the exact missing detail: an unknown about one property does not make another already-established fact uncertain or remove it from the decision scope. The scope itself is uncertain only if several readings of the episode genuinely remain possible."
                  ),
                },
                need: {
                  type: "string",
                  description: (
                    "Precise item the souvenirs must establish or complete before responding."
                  ),
                },
                handling: {
                  type: "object",
                  description: (
                    "Pre-reading conduct hypothesis. It will be reassessed from the exact bodies and must never prevent opening them."
                  ),
                  properties: {
                    mode: {
                      type: "string",
                      enum: ["direct", "clarify", "condition"],
                      description: (
                        "Pre-reading hypothesis: if an unknown remains after the exact bodies, clarify asks the single question then waits without composing the decision that depends on it, and condition states the condition first. If reading resolves the unknown, direct becomes possible again."
                      ),
                    },
                    plan: {
                      type: "string",
                      description: (
                        "With clarify, the single exact natural question; the voice then waits without composing any dependent choice. With condition, the exact condition preceding any dependent proposal. With direct, a brief internal justification."
                      ),
                    },
                  },
                  required: ["mode", "plan"],
                  additionalProperties: false,
                },
              },
              required: ["established", "uncertain", "need", "handling"],
              additionalProperties: false,
            },
          },
          required: ["paths", "scene"],
          additionalProperties: false,
        },
        daemonUrl: toolDaemonUrl,
        userId: toolUserId,
        context,
      }),
      { name: SOUVENIR_READ_TOOL },
    );
    api.registerTool(
      (context) => createMemoryTool({
        name: L0_RECALL_TOOL,
        description: (
          "Exceptional, bounded search in exact past personal exchanges. It can verify a missing personal fact or recover a precise chronological boundary. Use only with a defensible temporal or conversational basis. Not for the web or external facts." +
          CODEX_DIRECT_JSON_TOOL_RESULT_NOTE
        ),
        parameters: {
          type: "object",
          properties: {
            need: {
              type: "string",
              description: (
                "A single atomic personal item missing from the response, with its attribution. Split distinct needs into separate calls."
              ),
            },
            lookup_kind: {
              type: "string",
              enum: ["personal_fact", "conversation_chronology"],
              description: (
                "`personal_fact` verifies an attributed item and first consults plausible souvenirs. `conversation_chronology` retrieves what was discussed immediately before or after a branching point and accesses the exact L0 order directly."
              ),
            },
          },
          required: ["need"],
          additionalProperties: false,
        },
        daemonUrl: toolDaemonUrl,
        userId: toolUserId,
        context,
      }),
      { name: L0_RECALL_TOOL },
    );
  }

  api.on("before_prompt_build", async (_event, ctx) => {
    const state = turnCache.get(ctx?.sessionId || "");
    if (
      state?.technicalHeartbeat &&
      Date.now() - state.updatedAt <= TURN_CACHE_TTL_MS
    ) {
      writeOpenClawRuntimeHealth({
        sessionId: ctx?.sessionId || "unknown",
        status: "healthy",
        reason: "technical_heartbeat_bypassed",
      });
      writeHostCapabilityEvent("prompt_heartbeat_bypassed", {
        sovereign_voice: true,
      });
      return;
    }
    if (
      !state ||
      Date.now() - state.updatedAt > TURN_CACHE_TTL_MS ||
      !state.sovereignSystemPrompt
    ) {
      writeOpenClawRuntimeHealth({
        sessionId: ctx?.sessionId || "unknown",
        status: "degraded",
        reason: "canonical_prompt_unavailable",
      });
      api.logger.warn?.(
        "[daemon-harness] sovereign system unavailable; OpenClaw baseline retained",
      );
      writeHostCapabilityEvent("prompt_system_fallback", {
        sovereign_voice: true,
      });
      return;
    }
    // OpenClaw first adds the context-engine `systemPromptAddition` to its
    // developer instructions, then a `systemPrompt` returned by this hook
    // replaces those instructions. Recompose the three canonical instruction
    // tiers in Hermes order. The near-turn tier remains an instruction here;
    // it must never be flattened into a synthetic user message.
    const leadingSystemPrompt = state.sovereignDeveloperBackground
      ? state.structuredPromptTiers
        ? `${state.sovereignSystemPrompt}\n\n${state.sovereignDeveloperBackground.trim()}`
        : `${state.sovereignDeveloperBackground.trim()}\n\n${state.sovereignSystemPrompt}`
      : state.sovereignSystemPrompt;
    const effectiveSystemPrompt = state.sovereignNearTurnPrompt
      ? `${leadingSystemPrompt}\n\n${state.sovereignNearTurnPrompt.trim()}`
      : leadingSystemPrompt;
    api.logger.info?.(
      `[daemon-harness] sovereign system applied | session=${ctx?.sessionId || "unknown"}` +
        ` | after_assemble_ms=${state.assembleCompletedAt ? Date.now() - state.assembleCompletedAt : "unknown"}` +
        ` | background_chars=${state.sovereignDeveloperBackground.length}`,
    );
    writeOpenClawRuntimeHealth({
      sessionId: ctx?.sessionId || "unknown",
      status: "healthy",
      reason: "canonical_prompt_applied",
    });
    state.promptAppliedAt = Date.now();
    writeHostCapabilityEvent("prompt_system_replaced", {
      sovereign_voice: true,
      sovereign_library: Boolean(state.memoryToolsActive),
      system_chars: effectiveSystemPrompt.length,
      primary_system_chars: state.sovereignSystemPrompt.length,
      background_chars: state.sovereignDeveloperBackground.length,
      near_turn_chars: state.sovereignNearTurnPrompt.length,
    });
    return {
      systemPrompt: effectiveSystemPrompt,
    };
  });

  api.on("message_sent", (event, context) => {
    const sessionKey = context?.sessionKey || context?.sessionId || "";
    if (sessionKey) degradedDeliveryCache.delete(sessionKey);
  });

  api.registerContextEngine(ENGINE_ID, (ctx) => {
    // ctx.config is the FULL OpenClawConfig, not the plugin configuration.
    // The latter lives under plugins.entries[<id>].config.
    // Reading ctx.config.userId once returned undefined (2026-07-13),
    // triggering the "default" fallback and writing memory for the wrong user.
    const fullConfig = (ctx && ctx.config) || {};
    const pluginConfig =
      (fullConfig.plugins &&
        fullConfig.plugins.entries &&
        fullConfig.plugins.entries[ENGINE_ID] &&
        fullConfig.plugins.entries[ENGINE_ID].config) ||
      {};
    const daemonUrl = (pluginConfig.daemonUrl || process.env.DAEMON_HARNESS_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
    const userId = pluginConfig.userId || process.env.DAEMON_CORE_USER_ID || "default";
    const log = (ctx && ctx.log) || console;

    return {
      info: {
        id: ENGINE_ID,
        name: "Daemon Harness",
        // ownsCompaction=false: OpenClaw's native compactor owns compaction.
        // Personal memory remains on the daemon side.
        ownsCompaction: false,
        acceptedHostParams: ["prompt", "runtimeSettings"],
        transcriptSemantics: {
          currentTurnFence: "before-current-turn-entry-v1",
          turnAdvancementIdempotency: "atomic-idempotent-v1",
        },
      },

      // Persist messages. This is the sole writer of user OR
      // assistant messages, regardless of call order. Assembly stays read-only.
      async ingest({ sessionId, message, isHeartbeat }) {
        if (isHeartbeat || !message) return { ingested: false };
        if (message.role !== "user" && message.role !== "assistant") {
          return { ingested: false };
        }
        const role = message.role;
        const rawContent = messageText(message);
        const content = role === "assistant"
          ? cleanVisibleAssistantText(rawContent)
          : rawContent;
        if (!content) return { ingested: false };
        const state = turnState(sessionId);
        if (role === "user" && state) state.lastUserMessage = content;
        const rawIdempotencyKey = (
          message.idempotencyKey || message.idempotency_key || ""
        );
        const idempotencyKey = rawIdempotencyKey
          ? `${userId}:${role}:${String(rawIdempotencyKey)}`
          : "";
        const previousIngest = idempotencyKey
          ? ingestCache.get(idempotencyKey)
          : null;
        if (previousIngest) {
          if (role === "user" && state && previousIngest.l0Ref) {
            state.userL0Ref = previousIngest.l0Ref;
          }
          return previousIngest.result || { ingested: true };
        }
        if (idempotencyKey) {
          ingestCache.set(idempotencyKey, {
            updatedAt: Date.now(),
            l0Ref: {},
            result: null,
          });
        }
        try {
          const data = await postJson(`${daemonUrl}/openclaw/ingest`, {
            user_id: userId,
            role,
            content,
            last_user_message:
              role === "assistant" ? state?.lastUserMessage || "" : "",
            selected_souvenirs:
              role === "assistant" ? state?.selectedSouvenirs || [] : [],
            route: role === "assistant" ? state?.route || {} : {},
            voice_path: role === "assistant" ? state?.voicePath || "" : "",
            canary_trace_id: state?.canaryTraceId || "",
            source_turn_started_at:
              role === "assistant" ? state?.sourceTurnStartedAt || 0 : 0,
            scene_scope:
              role === "assistant" ? state?.sceneScope || "" : "",
            read_souvenirs:
              role === "assistant" ? state?.readSouvenirs || [] : [],
            session_id: sessionId || "",
            idempotency_key: rawIdempotencyKey
              ? String(rawIdempotencyKey)
              : "",
            user_l0_ref:
              role === "assistant" ? state?.userL0Ref || {} : {},
          });
          if (role === "user" && state) {
            state.userL0Ref =
              data.l0_ref && typeof data.l0_ref === "object"
                ? data.l0_ref
                : {};
          }
          if (role === "assistant" && state) {
            log.info?.(
              `[daemon-harness] turn completed | session=${sessionId || "unknown"}` +
                ` | route=${state.route?.mode || "unknown"}` +
                ` | since_assemble_ms=${state.assembleStartedAt ? Date.now() - state.assembleStartedAt : "unknown"}` +
                ` | since_prompt_ms=${state.promptAppliedAt ? Date.now() - state.promptAppliedAt : "unknown"}`,
            );
          }
          if (role === "assistant") {
            writeHostCapabilityEvent("assistant_ingest_completed", {
              ingested: true,
              sovereign_library: Boolean(state?.memoryToolsActive),
            });
          }
          if (role === "assistant" && sessionId) turnCache.delete(sessionId);
          const result = { ingested: true };
          if (idempotencyKey) {
            ingestCache.set(idempotencyKey, {
              updatedAt: Date.now(),
              l0Ref:
                data.l0_ref && typeof data.l0_ref === "object"
                  ? data.l0_ref
                  : {},
              result,
            });
          }
          return result;
        } catch (err) {
          if (idempotencyKey) ingestCache.delete(idempotencyKey);
          if (role === "assistant") {
            writeHostCapabilityEvent("assistant_ingest_completed", {
              ingested: false,
              sovereign_library: Boolean(state?.memoryToolsActive),
            });
          }
          log.warn?.(`[daemon-harness] ingest failed: ${err.message}`);
          return { ingested: false }; // ne jamais casser le tour d'OpenClaw
        }
      },

      // OpenClaw 2026.8+ supplies only the accepted turn here, from the
      // user's input to the terminal response. The same advancementKey can
      // be replayed after a failure, so both writes share its daemon-side
      // idempotency scope. The local cache is only a process-local shortcut;
      // durable idempotency remains the responsibility of L0.
      async commitTurn({
        advancementKey,
        admission,
        messages,
        sessionId,
        isHeartbeat,
      }) {
        if (isHeartbeat) return { status: "committed" };
        const key = String(advancementKey || "").trim();
        if (!key) throw new Error("OpenClaw advancement key is required");
        const commitCacheKey = `${userId}:turn:${key}`;
        if (ingestCache.has(commitCacheKey)) {
          return { status: "duplicate" };
        }
        let durableMessages = normalizedDurableMessages(messages);
        let recoveredFromTranscript = false;
        let userMessage = durableMessages.find(
          (message) => message.role === "user",
        );
        let assistantMessage = durableMessages.findLast(
          (message) => message.role === "assistant",
        );
        if (!userMessage || !assistantMessage) {
          const recovery = await readAcceptedTurnFromOpenClawTranscript(admission);
          if (recovery.kind === "recovered") {
            recoveredFromTranscript = true;
            durableMessages = normalizedDurableMessages(recovery.messages);
            userMessage = durableMessages.find(
              (message) => message.role === "user",
            );
            assistantMessage = durableMessages.findLast(
              (message) => message.role === "assistant",
            );
            recordOpenClawRuntimeIncident({
              advancementKey: key,
              outcome: "recovered",
              reason: "accepted_turn_rebuilt_from_transcript",
            });
            log.warn?.(
              "[daemon-harness] incomplete accepted turn rebuilt from the OpenClaw transcript",
            );
          } else if (recovery.kind === "closed_without_assistant") {
            recordOpenClawRuntimeIncident({
              advancementKey: key,
              outcome: "quarantined",
              reason: "accepted_turn_has_no_assistant_before_next_user",
            });
            writeOpenClawRuntimeHealth({
              sessionId: sessionId || admission?.sessionId || "unknown",
              status: "degraded",
              reason: "accepted_turn_quarantined",
            });
            log.warn?.(
              "[daemon-harness] incomplete accepted turn quarantined; later turns remain available",
            );
            // The native transcript remains the repair source. Acknowledging here
            // prevents one unrecoverable turn from disabling memory
            // for every subsequent turn.
            return { status: "committed" };
          }
        }
        if (!userMessage || !assistantMessage) {
          recordOpenClawRuntimeIncident({
            advancementKey: key,
            outcome: "deferred",
            reason: "accepted_turn_not_closed_yet",
          });
          throw new Error("OpenClaw accepted turn is incomplete and not recoverable yet");
        }
        const state = turnState(sessionId);
        state.lastUserMessage = userMessage.content;
        const historicalRecovery = (
          recoveredFromTranscript &&
          userMessage.occurredAt > 0 &&
          (Date.now() / 1000) - userMessage.occurredAt > 300
        );
        const common = {
          user_id: userId,
          selected_souvenirs: state.selectedSouvenirs || [],
          route: state.route || {},
          voice_path: state.voicePath || "",
          canary_trace_id: state.canaryTraceId || "",
          source_turn_started_at: state.sourceTurnStartedAt || 0,
          scene_scope: state.sceneScope || "",
          read_souvenirs: state.readSouvenirs || [],
          session_id: sessionId || "",
          idempotency_key: key,
          historical_recovery: historicalRecovery,
        };
        const userData = await postJson(`${daemonUrl}/openclaw/ingest`, {
          ...common,
          role: "user",
          content: userMessage.content,
          occurred_at: userMessage.occurredAt,
        });
        await postJson(`${daemonUrl}/openclaw/ingest`, {
          ...common,
          role: "assistant",
          content: assistantMessage.content,
          occurred_at: assistantMessage.occurredAt,
          last_user_message: userMessage.content,
          user_l0_ref:
            userData.l0_ref && typeof userData.l0_ref === "object"
              ? userData.l0_ref
              : {},
        });
        writeHostCapabilityEvent("assistant_ingest_completed", {
          ingested: true,
          sovereign_library: Boolean(state.memoryToolsActive),
          committed_turn: true,
        });
        ingestCache.set(commitCacheKey, {
          updatedAt: Date.now(),
          result: { status: "committed" },
        });
        if (!recoveredFromTranscript) {
          scheduleRecentTranscriptReconciliation({
            admission,
            daemonUrl,
            userId,
            log,
          });
        }
        if (sessionId) turnCache.delete(sessionId);
        return { status: "committed" };
      },

      // Reading: inject daemon context through systemPromptAddition
      // and supplement the native history only with recent canonical
      // turns missing from it.
      async assemble({ sessionId, messages, tokenBudget, prompt, runtimeSettings }) {
        const hostMessages = Array.isArray(messages) ? messages : [];
        const deltaOnlyProjection = projectsIntoPersistentCodexThread(runtimeSettings);
        if (isTechnicalHeartbeat(prompt, messages)) {
          const heartbeatState = turnState(sessionId);
          if (heartbeatState) {
            heartbeatState.technicalHeartbeat = true;
            heartbeatState.assembleStartedAt = Date.now();
            heartbeatState.updatedAt = Date.now();
          }
          writeHostCapabilityEvent("heartbeat_bypassed", {
            sovereign_voice: true,
            context_projection: deltaOnlyProjection ? "delta_only" : "full",
          });
          return {
            // The Codex app-server already owns its native thread. Replaying
            // the same transcript in the current prompt grows on every turn.
            messages: deltaOnlyProjection ? [] : hostMessages,
            estimatedTokens: deltaOnlyProjection ? 0 : estimateTokens(hostMessages),
            systemPromptAddition: "",
            ...(deltaOnlyProjection
              ? {
                  contextProjection: {
                    mode: "thread_bootstrap",
                    epoch: `daemon-heartbeat:${opaqueRuntimeRef(sessionId)}`,
                    fingerprint: CODEX_TURN_PROJECTION_VERSION,
                  },
                }
              : {}),
          };
        }
        const previous = sessionId ? turnCache.get(sessionId) : null;
        // User ingest can precede assembly: preserve its trace_id.
        // State that already started assembly belongs to the preceding turn.
        if (sessionId && previous?.assembleStartedAt) {
          turnCache.delete(sessionId);
        }
        if (sessionId) degradedDeliveryCache.delete(sessionId);
        const state = turnState(sessionId);
        if (state) state.technicalHeartbeat = false;
        // On the Codex path, OpenClaw may pass its rendered host prompt here,
        // including metadata, a long conversation and the current request,
        // rather than the incoming message alone. The envelope helps the
        // voice, but must never become the embeddings query.
        // Prefer the ingested L0 message; otherwise strip only the
        // explicitly delimited OpenClaw envelope.
        const userMessage = retrievalUserMessage({ prompt, messages, state });
        if (state && userMessage) state.lastUserMessage = userMessage;
        if (state) state.assembleStartedAt = Date.now();
        writeHostCapabilityEvent("assemble_started", {
          sovereign_voice: true,
          sovereign_library: true,
        });
        let systemPromptAddition = "";
        let assembledMessages = deltaOnlyProjection ? [] : hostMessages;
        if (userMessage) {
          try {
            const data = await postJson(`${daemonUrl}/openclaw/assemble`, {
              user_id: userId,
              message: userMessage,
              session_id: sessionId || "",
              trace_id: state?.canaryTraceId || "",
            });
            const sourceCollapsedSystemPrompt =
              data.sovereign_system_prompt || "";
            const structuredPromptTiers = Boolean(
              data.primary_system_prompt && data.near_turn_system_prompt,
            );
            const sourceSovereignSystemPrompt = structuredPromptTiers
              ? data.primary_system_prompt
              : sourceCollapsedSystemPrompt;
            const sourceNearTurnSystemPrompt =
              data.near_turn_system_prompt || "";
            const sovereignSystemPrompt = withOpenClawProgressPreamble(
              projectSovereignPromptForOpenClaw(sourceSovereignSystemPrompt),
            );
            const nearTurnSystemPrompt = projectSovereignPromptForOpenClaw(
              sourceNearTurnSystemPrompt,
            );
            // The daemon can separate the main sovereign context from
            // persistent background context. The server guarantees disjoint
            // outputs; OpenClaw places the addition at developer level
            // without changing the system prompt or tools.
            systemPromptAddition = data.system_prompt_addition || "";
            const dialogueMerge = mergeCanonicalRecentDialogue(
              hostMessages,
              data.canonical_recent_dialogue,
            );
            const canonicalProjection = Array.isArray(
              data.canonical_recent_dialogue,
            )
              ? mergeCanonicalRecentDialogue(
                  [],
                  data.canonical_recent_dialogue,
                ).messages
              : hostMessages;
            // The Codex thread is renewed for each user turn, so it receives
            // the complete canonical dialogue supplied by the daemon,
            // as Hermes does, instead of OpenClaw's previous internal history.
            // Other hosts retain their existing behavior.
            assembledMessages = deltaOnlyProjection
              ? canonicalProjection
              : dialogueMerge.messages;
            if (state) {
              state.selectedSouvenirs = Array.isArray(data.souvenirs)
                ? data.souvenirs
                : [];
              state.route = data.route && typeof data.route === "object"
                ? data.route
                : {};
              state.sovereignSystemPrompt = sovereignSystemPrompt;
              state.sovereignDeveloperBackground = systemPromptAddition;
              state.sovereignNearTurnPrompt = nearTurnSystemPrompt;
              state.structuredPromptTiers = structuredPromptTiers;
              state.voicePath = data.voice_path || (
                sovereignSystemPrompt ? "canonical" : "host_fallback"
              );
              state.degraded = data.degraded === true;
              state.degradationReason = data.degradation_reason || "";
              state.memoryToolsActive =
                state.voicePath === "canonical" &&
                !state.degraded;
              state.canaryTraceId =
                data.canary_trace_id || state.canaryTraceId || "";
              state.sourceTurnStartedAt = Number(data.source_turn_started_at) || 0;
              state.sceneScope = data.scene_scope || "";
              state.readSouvenirs = [];
              state.libraryPreflightCalls = 0;
              state.lastMemoryToolResult = null;
              state.assembleCompletedAt = Date.now();
              state.updatedAt = Date.now();
              if (sessionId && state.degraded) {
                degradedDeliveryCache.set(sessionId, {
                  degraded: true,
                  degradationReason: state.degradationReason,
                  updatedAt: Date.now(),
                });
              } else if (sessionId) {
                degradedDeliveryCache.delete(sessionId);
              }
            }
            log.info?.(
              `[daemon-harness] assemble completed | session=${sessionId || "unknown"}` +
                ` | route=${data.route?.mode || "unknown"}` +
                ` | depth=${data.depth || "unknown"}` +
                ` | souvenirs=${Array.isArray(data.souvenirs) ? data.souvenirs.length : 0}` +
                ` | selection_source=${data.selection_source || "none"}` +
                ` | radar_complete=${Boolean(data.route?.radar_final_complete)}` +
                ` | sovereign=${Boolean(sovereignSystemPrompt)}` +
                ` | canonical_dialogue=${dialogueMerge.canonicalCount}` +
                ` | canonical_dialogue_added=${dialogueMerge.added}` +
                ` | context_projection=${deltaOnlyProjection ? "delta_only" : "full"}` +
                ` | projected_messages=${assembledMessages.length}` +
                ` | near_turn=${Boolean(nearTurnSystemPrompt)}` +
                ` | voice_path=${data.voice_path || "canonical"}` +
                ` | prompt_variant=${data.prompt_variant || "current"}` +
                ` | http_ms=${state?.assembleStartedAt ? Date.now() - state.assembleStartedAt : "unknown"}` +
                ` | daemon_ms=${compactTimings(data.timings_ms)}`,
            );
            writeHostCapabilityEvent("assemble_completed", {
              sovereign_voice: true,
              sovereign_library: true,
              voice_path: data.voice_path || "canonical",
              route_mode: data.route?.mode || "unknown",
              capsule_count: Array.isArray(data.souvenirs) ? data.souvenirs.length : 0,
              addition_chars: systemPromptAddition.length,
              source_sovereign_system_chars:
                sourceSovereignSystemPrompt.length,
              sovereign_system_chars: sovereignSystemPrompt.length,
              progress_preamble: true,
              source_near_turn_chars: sourceNearTurnSystemPrompt.length,
              near_turn_chars: nearTurnSystemPrompt.length,
              canonical_dialogue_count: dialogueMerge.canonicalCount,
              canonical_dialogue_added: dialogueMerge.added,
              context_projection: deltaOnlyProjection ? "delta_only" : "full",
              projected_message_count: assembledMessages.length,
              duration_ms: state?.assembleStartedAt
                ? Date.now() - state.assembleStartedAt
                : null,
              daemon_timings_ms: data.timings_ms || {},
            });
          } catch (err) {
            writeOpenClawRuntimeHealth({
              sessionId: sessionId || "unknown",
              status: "degraded",
              reason: "daemon_bridge_unavailable",
            });
            systemPromptAddition = DEGRADED_HOST_CONTRACT;
            if (state) {
              state.selectedSouvenirs = [];
              state.route = {
                mode: "degraded",
                reason: "Daemon bridge unavailable for this turn.",
              };
              state.sovereignSystemPrompt = "";
              state.sovereignDeveloperBackground = "";
              state.sovereignNearTurnPrompt = "";
              state.structuredPromptTiers = false;
              state.voicePath = "host_fallback";
              state.memoryToolsActive = false;
              state.degraded = true;
              state.degradationReason = "daemon_bridge_unavailable";
              state.assembleCompletedAt = Date.now();
              state.updatedAt = Date.now();
              if (sessionId) {
                degradedDeliveryCache.set(sessionId, {
                  degraded: true,
                  degradationReason: state.degradationReason,
                  updatedAt: Date.now(),
                });
              }
            }
            writeHostCapabilityEvent("assemble_completed", {
              sovereign_voice: true,
              sovereign_library: true,
              voice_path: "host_fallback",
              degraded: true,
              degradation_reason: "daemon_bridge_unavailable",
              status: "error",
              duration_ms: state?.assembleStartedAt
                ? Date.now() - state.assembleStartedAt
                : null,
            });
            log.warn?.(`[daemon-harness] assemble failed: ${err.message}`);
            // Explicit native fallback: no previous memory system prompt.
          }
        }
        return {
          messages: assembledMessages,
          estimatedTokens:
            estimateTokens(assembledMessages) +
            Math.ceil(String(state?.sovereignNearTurnPrompt || "").length / 4),
          systemPromptAddition,
          ...(deltaOnlyProjection
            ? { contextProjection: codexTurnProjection(state, assembledMessages) }
            : {}),
        };
      },

      // Delegate compaction to native OpenClaw (ownsCompaction=false).
      async compact() {
        return { ok: true, compacted: false };
      },
    };
  });
}

export const __testing = {
  clearTurnCache() {
    turnCache.clear();
    ingestCache.clear();
    degradedDeliveryCache.clear();
    transcriptReconciliationCache.clear();
  },
  stripInternalTurnMetadata,
  cleanAssistantMessage,
  cleanCodexLlmOutput,
  isTransientOpenClawToolFailure,
  isOpenClawProgressSummary,
  deleteTelegramMessageViaBotApi,
  createTelegramProgressAnchorStore,
  createTelegramProgressCleaner,
  mergeCanonicalRecentDialogue,
  readRecentClosedTurnsFromOpenClawTranscript,
  normalizeSouvenirReadArguments,
  projectSovereignPromptForOpenClaw,
  withOpenClawProgressPreamble,
};
