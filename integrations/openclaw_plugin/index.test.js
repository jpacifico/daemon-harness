import assert from "node:assert/strict";
import {
  mkdtempSync,
  readFileSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";

import register, { __testing } from "./index.js";

const SOUVENIR_READ_TOOL_CONTRACT = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/souvenir_read_tool_contract.json", import.meta.url),
    "utf8",
  ),
);
const L0_RECALL_TOOL_CONTRACT = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/l0_recall_tool_contract.json", import.meta.url),
    "utf8",
  ),
);

function response(body, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    async json() {
      return body;
    },
  };
}

function applyOutputTransforms(textTransforms, text) {
  return textTransforms.output.reduce(
    (current, replacement) => current.replace(replacement.from, replacement.to),
    text,
  );
}

function withProgressPreamble(source) {
  return __testing.withOpenClawProgressPreamble(source);
}

function setup({
  legacySovereignVoice,
  legacySovereignLibrary,
} = {}) {
  let engineFactory;
  let promptHook;
  let messageSendingHook;
  let beforeMessageWriteHook;
  let llmOutputHook;
  let beforeToolCallHook;
  let afterToolCallHook;
  let replyDispatchHook;
  let replyDispatchOptions;
  let textTransforms;
  const logs = [];
  const toolFactories = new Map();
  const api = {
    pluginConfig: {
      daemonUrl: "http://127.0.0.1:8000",
      userId: "u",
      ...(legacySovereignVoice === undefined
        ? {}
        : { sovereignVoice: legacySovereignVoice }),
      ...(legacySovereignLibrary === undefined
        ? {}
        : { sovereignLibrary: legacySovereignLibrary }),
    },
    logger: {
      info(message) { logs.push(message); },
      warn(message) { logs.push(message); },
    },
    registerContextEngine(_id, factory) {
      engineFactory = factory;
    },
    registerTool(factory, options) {
      toolFactories.set(options.name, factory);
    },
    registerTextTransforms(transforms) {
      textTransforms = transforms;
    },
    on(name, handler, options) {
      if (name === "before_prompt_build") promptHook = handler;
      if (name === "before_tool_call") beforeToolCallHook = handler;
      if (name === "after_tool_call") afterToolCallHook = handler;
      if (name === "message_sending") messageSendingHook = handler;
      if (name === "before_message_write") beforeMessageWriteHook = handler;
      if (name === "llm_output") llmOutputHook = handler;
      if (name === "reply_dispatch") {
        replyDispatchHook = handler;
        replyDispatchOptions = options;
      }
    },
  };
  register(api);
  const engine = engineFactory({
    config: {
      plugins: {
        entries: {
          "daemon-harness": {
            config: {
              daemonUrl: "http://127.0.0.1:8000",
              userId: "u",
              ...(legacySovereignVoice === undefined
                ? {}
                : { sovereignVoice: legacySovereignVoice }),
              ...(legacySovereignLibrary === undefined
                ? {}
                : { sovereignLibrary: legacySovereignLibrary }),
            },
          },
        },
      },
    },
    log: {
      info(message) { logs.push(message); },
      warn(message) { logs.push(message); },
    },
  });
  return {
    engine,
    promptHook,
    messageSendingHook,
    beforeMessageWriteHook,
    llmOutputHook,
    beforeToolCallHook,
    afterToolCallHook,
    replyDispatchHook,
    replyDispatchOptions,
    textTransforms,
    logs,
    tool(name, sessionId) {
      const factory = toolFactories.get(name);
      return factory ? factory({ sessionId }) : null;
    },
  };
}

test.beforeEach(() => {
  __testing.clearTurnCache();
});

test("OpenClaw projection removes host-owned planning and tool orchestration", () => {
  const prompt = [
    "# Identité",
    "",
    "Voix canonique.",
    "",
    "# Outils de l'hote",
    "",
    "Choisis et relance les outils selon cette procédure.",
    "",
    "# Seuil du tour",
    "",
    "Reste exacte et réponds au mouvement présent.",
    "",
    "# Mémoires exactes disponibles sur demande",
    "",
    "Sacha a été déposé ce matin.",
    "",
    "# Catalogue de la mémoire personnelle",
    "",
    "- Vie familiale : état récent et faits confirmés.",
    "",
    "## Frontière des connaissances externes",
    "",
    "Commence par une recherche large puis change de source.",
    "",
    "## État exact des sources connectées",
    "",
    "Calendrier connecté et disponible.",
    "",
    "# Dernière règle canonique",
    "",
    "Ce bloc de premier niveau reste présent.",
  ].join("\n");

  const projected = __testing.projectSovereignPromptForOpenClaw(prompt);

  assert.equal(projected.includes("# Outils de l'hote"), false);
  assert.equal(projected.includes("Choisis et relance"), false);
  assert.equal(projected.includes("Frontière des connaissances externes"), false);
  assert.equal(projected.includes("recherche large"), false);
  assert.equal(projected.includes("# Seuil du tour"), false);
  assert.equal(projected.includes("mouvement présent"), false);
  assert.match(projected, /# Identité/);
  assert.match(projected, /Voix canonique/);
  assert.match(projected, /# Mémoires exactes disponibles sur demande/);
  assert.match(projected, /Sacha a été déposé ce matin/);
  assert.match(projected, /# Catalogue de la mémoire personnelle/);
  assert.match(projected, /Vie familiale/);
  assert.match(projected, /## État exact des sources connectées/);
  assert.match(projected, /Calendrier connecté/);
  assert.match(projected, /# Dernière règle canonique/);
  assert.match(projected, /Ce bloc de premier niveau reste présent/);
});

test("OpenClaw projection preserves exact source status below the turn threshold", () => {
  const prompt = [
    "# Seuil du tour",
    "",
    "Planification immédiate déjà possédée par l'hôte.",
    "",
    "## Frontière des connaissances externes",
    "",
    "Stratégie externe déjà possédée par l'hôte.",
    "",
    "## État exact des sources connectées",
    "",
    "Le calendrier indique exactement un rendez-vous à 14 h.",
  ].join("\n");

  const projected = __testing.projectSovereignPromptForOpenClaw(prompt);

  assert.equal(projected.includes("# Seuil du tour"), false);
  assert.equal(projected.includes("Planification immédiate"), false);
  assert.equal(projected.includes("Frontière des connaissances externes"), false);
  assert.equal(projected.includes("Stratégie externe"), false);
  assert.match(projected, /## État exact des sources connectées/);
  assert.match(projected, /rendez-vous à 14 h/);
});

test("promoted progress preamble is standard and idempotent", () => {
  const prompt = "SYSTEME CANONIQUE";
  const enabled = __testing.withOpenClawProgressPreamble(prompt);
  const repeated = __testing.withOpenClawProgressPreamble(enabled);

  assert.equal(repeated, enabled);
  assert.match(enabled, /visible commentary a single/);
  assert.match(enabled, /without an unverified fact or tool name/);
  assert.match(enabled, /Without a tool, respond directly/);
});

test("memory tools stay directly visible under OpenClaw Tool Search", () => {
  const tools = setup();
  for (const name of ["daemon_read_souvenirs", "daemon_recall_l0"]) {
    assert.equal(tools.tool(name, "session-direct").catalogMode, "direct-only");
  }
});

test("exact souvenir read contract matches the shared Hermes fixture", () => {
  const memoryTool = setup().tool("daemon_read_souvenirs", "session-parity");
  const properties = memoryTool.parameters.properties;
  const scene = properties.scene;
  const sceneProperties = scene.properties;
  const handling = sceneProperties.handling;
  const handlingProperties = handling.properties;

  assert.deepEqual(
    {
      description: memoryTool.description.slice(
        0,
        SOUVENIR_READ_TOOL_CONTRACT.description.length,
      ),
      paths_description: properties.paths.description,
      collection_path_description: properties.collection_path.description,
      collection_offset_description: properties.collection_offset.description,
      scene_description: scene.description,
      established_description: sceneProperties.established.description,
      uncertain_description: sceneProperties.uncertain.description,
      need_description: sceneProperties.need.description,
      handling_description: handling.description,
      mode_description: handlingProperties.mode.description,
      plan_description: handlingProperties.plan.description,
    },
    SOUVENIR_READ_TOOL_CONTRACT,
  );
  assert.match(memoryTool.description, /returns a JSON string directly/);
  assert.match(memoryTool.description, /do not look for a `.content` property/);
});

test("L0 recall contract matches the shared Hermes fixture", () => {
  const memoryTool = setup().tool("daemon_recall_l0", "session-l0-parity");
  const properties = memoryTool.parameters.properties;

  assert.deepEqual(
    {
      description: memoryTool.description.slice(
        0,
        L0_RECALL_TOOL_CONTRACT.description.length,
      ),
      lookup_kind_description: properties.lookup_kind.description,
      need_description: properties.need.description,
    },
    L0_RECALL_TOOL_CONTRACT,
  );
  assert.match(memoryTool.description, /returns a JSON string directly/);
  assert.match(memoryTool.description, /do not look for a `.content` property/);
});

test("souvenir arguments repair only an unambiguous misplaced need", () => {
  const valid = {
    paths: ["theme/example"],
    scene: {
      established: ["Cadre établi"],
      uncertain: ["Pièce manquante"],
      need: "Pièce exacte",
      handling: { mode: "direct", plan: "Lire la souvenir." },
    },
  };
  const unchanged = __testing.normalizeSouvenirReadArguments(valid);
  assert.equal(unchanged.arguments, valid);
  assert.equal(unchanged.repaired, false);
  assert.equal(unchanged.conflict, false);

  const misplaced = {
    paths: ["theme/example"],
    need: "Pièce exacte",
    scene: {
      established: ["Cadre établi"],
      uncertain: ["Pièce manquante"],
      handling: { mode: "direct", plan: "Lire la souvenir." },
    },
  };
  const repaired = __testing.normalizeSouvenirReadArguments(misplaced);
  assert.equal(repaired.repaired, true);
  assert.equal(repaired.conflict, false);
  assert.equal(Object.hasOwn(repaired.arguments, "need"), false);
  assert.equal(repaired.arguments.scene.need, "Pièce exacte");
  assert.equal(Object.hasOwn(misplaced, "need"), true);
  assert.equal(Object.hasOwn(misplaced.scene, "need"), false);

  const absent = __testing.normalizeSouvenirReadArguments({
    paths: ["theme/example"],
    scene: {
      established: [],
      uncertain: [],
      handling: { mode: "direct", plan: "Lire la souvenir." },
    },
  });
  assert.equal(absent.repaired, false);
  assert.equal(absent.conflict, false);

  const conflicting = __testing.normalizeSouvenirReadArguments({
    ...valid,
    need: "Autre pièce",
  });
  assert.equal(conflicting.repaired, false);
  assert.equal(conflicting.conflict, true);
});

test("souvenir tool sends a repaired need and rejects a conflict locally", async () => {
  const { engine, tool } = setup();
  const requests = [];
  globalThis.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    requests.push({ url, body });
    if (url.endsWith("/assemble")) {
      return response({
        sovereign_system_prompt: "SYSTEME ATLAS",
        system_prompt_addition: "",
        voice_path: "canonical",
        canary_trace_id: "trace-repair",
        route: { mode: "canonical" },
      });
    }
    if (url.endsWith("/tool")) {
      return response({ result: { status: "ok", capsules: [] } });
    }
    return response({ ingested: true });
  };
  await engine.ingest({
    sessionId: "session-repair",
    message: { role: "user", content: "Question" },
  });
  await engine.assemble({
    sessionId: "session-repair",
    messages: [],
    prompt: "Question",
  });
  const memoryTool = tool("daemon_read_souvenirs", "session-repair");
  const scene = {
    established: ["Cadre établi"],
    uncertain: ["Pièce manquante"],
    handling: { mode: "direct", plan: "Lire la souvenir." },
  };
  const repaired = await memoryTool.execute("call-repair", {
    paths: ["theme/example"],
    need: "Pièce exacte",
    scene,
  });
  const toolRequests = requests.filter((entry) => entry.url.endsWith("/tool"));
  assert.equal(repaired.details.status, "ok");
  assert.equal(toolRequests.length, 1);
  assert.equal(Object.hasOwn(toolRequests[0].body.arguments, "need"), false);
  assert.equal(toolRequests[0].body.arguments.scene.need, "Pièce exacte");

  const conflict = await memoryTool.execute("call-conflict", {
    paths: ["theme/example"],
    need: "Autre pièce",
    scene: { ...scene, need: "Pièce exacte" },
  });
  assert.equal(conflict.details.status, "error");
  assert.match(conflict.details.reason, /inconsistent/);
  assert.equal(requests.filter((entry) => entry.url.endsWith("/tool")).length, 1);
});

test("canonical recent dialogue fills an empty OpenClaw history", () => {
  const canonical = [
    {
      role: "user",
      content: "[heure locale du tour : 2026-08-24T09:00:00+02:00]\nTu te souviens ?",
    },
    { role: "assistant", content: "Oui, de cette discussion." },
  ];

  const merged = __testing.mergeCanonicalRecentDialogue([], canonical);

  assert.deepEqual(merged.messages, canonical);
  assert.deepEqual(merged.missing, canonical);
  assert.equal(merged.added, 2);
  assert.equal(merged.canonicalCount, 2);
});

test("canonical recent dialogue does not duplicate native matching turns", () => {
  const messages = [
    { role: "user", content: "Tu te souviens ?" },
    { role: "assistant", content: "Oui, de cette discussion." },
    { role: "user", content: "Et maintenant ?" },
  ];
  const canonical = [
    {
      role: "user",
      content: "[heure locale du tour : 2026-08-24T09:00:00+02:00]\nTu te souviens ?",
    },
    { role: "assistant", content: "Oui, de cette discussion." },
  ];

  const merged = __testing.mergeCanonicalRecentDialogue(messages, canonical);

  assert.strictEqual(merged.messages, messages);
  assert.deepEqual(merged.missing, []);
  assert.equal(merged.added, 0);
  assert.equal(merged.canonicalCount, 2);
});

test("canonical recent dialogue preserves every native OpenClaw message", () => {
  const nativeMessages = [
    { role: "system", content: "SYSTEME HOTE" },
    { role: "user", content: "Un tour déjà présent" },
    { role: "assistant", content: [{ type: "toolCall", name: "web_search" }] },
    { role: "toolResult", content: [{ type: "text", text: "résultat" }] },
    { role: "assistant", content: "Réponse déjà présente" },
    { role: "user", content: "Question courante" },
  ];
  const missing = { role: "assistant", content: "Tour venu de Hermes" };
  const canonical = [
    missing,
    { role: "user", content: "Un tour déjà présent" },
    { role: "assistant", content: "Réponse déjà présente" },
  ];

  const merged = __testing.mergeCanonicalRecentDialogue(nativeMessages, canonical);

  assert.deepEqual(merged.messages, [missing, ...nativeMessages]);
  assert.deepEqual(merged.missing, [missing]);
  for (const nativeMessage of nativeMessages) {
    assert.ok(merged.messages.includes(nativeMessage));
  }
  assert.equal(merged.added, 1);
  assert.equal(merged.canonicalCount, 3);
});

test("malformed canonical dialogue leaves native OpenClaw history untouched", () => {
  const messages = [{ role: "user", content: "Question courante" }];
  const merged = __testing.mergeCanonicalRecentDialogue(messages, [
    { role: "system", content: "Ne doit pas entrer" },
    { role: "assistant", content: { text: "forme non contractuelle" } },
  ]);

  assert.strictEqual(merged.messages, messages);
  assert.equal(merged.added, 0);
  assert.equal(merged.canonicalCount, 0);
});

test("assemble projects missing canonical dialogue without changing host tools", async () => {
  const { engine } = setup();
  const hostToolResult = {
    role: "toolResult",
    content: [{ type: "text", text: "résultat natif" }],
  };
  globalThis.fetch = async () => response({
    system_prompt_addition: "memoire",
    sovereign_system_prompt: "SYSTEME DAEMON",
    canonical_recent_dialogue: [
      { role: "user", content: "Tour partagé" },
      { role: "assistant", content: "Réponse partagée" },
    ],
    souvenirs: [],
    route: { mode: "canonical" },
  });

  const assembled = await engine.assemble({
    sessionId: "session-canonical-dialogue",
    messages: [hostToolResult, { role: "user", content: "Question courante" }],
    prompt: "Question courante",
  });

  assert.deepEqual(assembled.messages, [
    { role: "user", content: "Tour partagé" },
    { role: "assistant", content: "Réponse partagée" },
    hostToolResult,
    { role: "user", content: "Question courante" },
  ]);
  assert.ok(assembled.messages.includes(hostToolResult));
});

test("sovereign voice preserves Hermes prompt order through system replacement", async () => {
  const { engine, promptHook } = setup();
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return response({
      system_prompt_addition: "memoire",
      sovereign_system_prompt: "SYSTEME DAEMON\n\nSEUIL DU TOUR",
      primary_system_prompt: "SYSTEME DAEMON",
      near_turn_system_prompt: "SEUIL DU TOUR",
      souvenirs: ["theme/famille"],
      route: { mode: "contextual", reason: "famille" },
    });
  };

  const assembled = await engine.assemble({
    sessionId: "session-1",
    messages: [],
    prompt: "Parle-moi de ma famille",
  });
  const result = await promptHook(
    { prompt: "Parle-moi de ma famille", messages: [] },
    { sessionId: "session-1" },
  );

  assert.deepEqual(result, {
    systemPrompt: `${withProgressPreamble("SYSTEME DAEMON")}\n\nmemoire\n\nSEUIL DU TOUR`,
  });
  assert.equal(assembled.systemPromptAddition, "memoire");
  assert.equal(result.systemPrompt.split("memoire").length - 1, 1);
  assert.ok(
    result.systemPrompt.indexOf("SYSTEME DAEMON") <
      result.systemPrompt.indexOf("memoire"),
  );
  assert.ok(
    result.systemPrompt.indexOf("memoire") <
      result.systemPrompt.indexOf("SEUIL DU TOUR"),
  );
  assert.equal(Object.hasOwn(requests[0], "sovereign_voice"), false);
  assert.equal(Object.hasOwn(requests[0], "sovereign_library"), false);
});

test("promoted progress preamble changes only the OpenClaw system prompt", async () => {
  const { engine, promptHook } = setup();
  globalThis.fetch = async () => response({
    system_prompt_addition: "ARRIERE PLAN",
    sovereign_system_prompt: "SYSTEME DAEMON\n\nSEUIL DU TOUR",
    primary_system_prompt: "SYSTEME DAEMON",
    near_turn_system_prompt: "SEUIL DU TOUR",
    souvenirs: [],
    route: { mode: "canonical" },
  });

  await engine.assemble({
    sessionId: "session-progress-preamble",
    messages: [],
    prompt: "Vérifie cette information",
  });
  const result = await promptHook(
    { prompt: "Vérifie cette information", messages: [] },
    { sessionId: "session-progress-preamble" },
  );

  assert.ok(result.systemPrompt.startsWith("SYSTEME DAEMON\n\n"));
  assert.match(result.systemPrompt, /## OpenClaw progress preamble/);
  assert.ok(
    result.systemPrompt.indexOf("OpenClaw progress preamble") <
      result.systemPrompt.indexOf("ARRIERE PLAN"),
  );
  assert.ok(
    result.systemPrompt.indexOf("ARRIERE PLAN") <
      result.systemPrompt.indexOf("SEUIL DU TOUR"),
  );
});

test("Codex app-server starts each daemon turn from canonical history", async () => {
  const { engine, promptHook } = setup();
  const hostMessages = [
    { role: "user", content: "Nous étions déjà en train d'en parler." },
    { role: "assistant", content: "Oui, je garde le fil." },
  ];
  globalThis.fetch = async () => response({
    system_prompt_addition: "ARRIERE PLAN EXACT",
    sovereign_system_prompt: "NARRATIF EXACT\n\nSEUIL EXACT",
    primary_system_prompt: "NARRATIF EXACT",
    near_turn_system_prompt: "SEUIL EXACT",
    canonical_recent_dialogue: hostMessages,
    souvenirs: [],
    route: { mode: "canonical" },
  });

  const assembled = await engine.assemble({
    sessionId: "session-codex-native",
    messages: hostMessages,
    prompt: "Et maintenant ?",
    runtimeSettings: { executionHost: { id: "codex-app-server" } },
  });
  const promptResult = await promptHook(
    { prompt: "Et maintenant ?", messages: [] },
    { sessionId: "session-codex-native" },
  );

  assert.deepEqual(assembled.messages, hostMessages);
  assert.equal(
    assembled.estimatedTokens,
    Math.ceil(
      (
        "Nous étions déjà en train d'en parler.".length +
        "Oui, je garde le fil.".length
      ) / 4,
    ) + Math.ceil("SEUIL EXACT".length / 4),
  );
  assert.equal(assembled.systemPromptAddition, "ARRIERE PLAN EXACT");
  assert.equal(assembled.contextProjection.mode, "thread_bootstrap");
  assert.match(assembled.contextProjection.epoch, /^daemon-turn:/);
  assert.deepEqual(promptResult, {
    systemPrompt: `${withProgressPreamble("NARRATIF EXACT")}\n\nARRIERE PLAN EXACT\n\nSEUIL EXACT`,
  });
});

test("Codex app-server projects the same canonical dialogue as Hermes", async () => {
  const { engine, promptHook } = setup();
  const nativeUser = { role: "user", content: "Tour déjà natif" };
  const nativeAssistant = { role: "assistant", content: "Réponse déjà native" };
  const missingFromHermes = {
    role: "assistant",
    content: "Tour canonique venu de Hermes",
  };
  const hostMessages = [nativeUser, nativeAssistant];
  globalThis.fetch = async () => response({
    system_prompt_addition: "memoire",
    sovereign_system_prompt: "SYSTEME DAEMON\n\nSEUIL DU TOUR",
    primary_system_prompt: "SYSTEME DAEMON",
    near_turn_system_prompt: "SEUIL DU TOUR",
    canonical_recent_dialogue: [missingFromHermes, nativeUser, nativeAssistant],
    souvenirs: [],
    route: { mode: "canonical" },
  });

  const assembled = await engine.assemble({
    sessionId: "session-cross-host",
    messages: hostMessages,
    prompt: "Je reprends ici.",
    runtimeSettings: { executionHost: { id: "codex-app-server" } },
  });

  const promptResult = await promptHook(
    { prompt: "Je reprends ici.", messages: [] },
    { sessionId: "session-cross-host" },
  );

  assert.deepEqual(assembled.messages, [
    missingFromHermes,
    nativeUser,
    nativeAssistant,
  ]);
  assert.deepEqual(promptResult, {
    systemPrompt: `${withProgressPreamble("SYSTEME DAEMON")}\n\nmemoire\n\nSEUIL DU TOUR`,
  });
  assert.equal(assembled.contextProjection.mode, "thread_bootstrap");
  assert.match(assembled.contextProjection.epoch, /^daemon-turn:/);
});

test("Codex app-server rotates the backend thread between user turns", async () => {
  const { engine } = setup();
  globalThis.fetch = async () => response({
    system_prompt_addition: "memoire",
    sovereign_system_prompt: "SYSTEME DAEMON\n\nSEUIL DU TOUR",
    primary_system_prompt: "SYSTEME DAEMON",
    near_turn_system_prompt: "SEUIL DU TOUR",
    canonical_recent_dialogue: [],
    souvenirs: [],
    route: { mode: "canonical" },
  });

  const first = await engine.assemble({
    sessionId: "session-fresh-turns",
    messages: [],
    prompt: "Premier message",
    runtimeSettings: { executionHost: { id: "codex-app-server" } },
  });
  const second = await engine.assemble({
    sessionId: "session-fresh-turns",
    messages: [],
    prompt: "Deuxième message",
    runtimeSettings: { executionHost: { id: "codex-app-server" } },
  });

  assert.equal(first.contextProjection.mode, "thread_bootstrap");
  assert.equal(second.contextProjection.mode, "thread_bootstrap");
  assert.match(first.contextProjection.epoch, /^daemon-turn:/);
  assert.match(second.contextProjection.epoch, /^daemon-turn:/);
  assert.notEqual(first.contextProjection.epoch, second.contextProjection.epoch);
});

test("embedded OpenClaw hosts keep the complete assembled history", async () => {
  const { engine } = setup();
  const native = { role: "user", content: "Tour natif" };
  const missing = { role: "assistant", content: "Tour canonique absent" };
  globalThis.fetch = async () => response({
    system_prompt_addition: "memoire",
    sovereign_system_prompt: "SYSTEME DAEMON",
    canonical_recent_dialogue: [missing],
    souvenirs: [],
    route: { mode: "canonical" },
  });

  const assembled = await engine.assemble({
    sessionId: "session-embedded",
    messages: [native],
    prompt: "Continue.",
    runtimeSettings: { executionHost: { id: "openclaw-embedded" } },
  });

  assert.deepEqual(assembled.messages, [missing, native]);
});

test("Codex app-server heartbeat does not replay its native transcript", async () => {
  const { engine, promptHook, logs } = setup();
  const messages = [
    { role: "user", content: "[OpenClaw heartbeat poll]" },
    { role: "assistant", content: "HEARTBEAT_OK" },
    { role: "user", content: "[OpenClaw heartbeat poll]" },
  ];

  const assembled = await engine.assemble({
    sessionId: "session-heartbeat",
    messages,
    prompt: (
      "Read HEARTBEAT.md if it exists (workspace context). Follow it strictly. " +
      "If nothing needs attention, reply HEARTBEAT_OK."
    ),
    runtimeSettings: { executionHost: { id: "codex-app-server" } },
  });

  assert.deepEqual(assembled.messages, []);
  assert.equal(assembled.estimatedTokens, 0);
  assert.equal(assembled.systemPromptAddition, "");
  const nextHeartbeat = await engine.assemble({
    sessionId: "session-heartbeat",
    messages,
    prompt: "[OpenClaw heartbeat poll]",
    runtimeSettings: { executionHost: { id: "codex-app-server" } },
  });
  assert.equal(
    assembled.contextProjection.epoch,
    nextHeartbeat.contextProjection.epoch,
  );
  assert.equal(
    await promptHook({}, { sessionId: "session-heartbeat" }),
    undefined,
  );
  assert.ok(!logs.some((line) => line.includes("sovereign system unavailable")));
});

test("live OpenClaw envelope sends only the current request to retrieval", async () => {
  const { engine } = setup();
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return response({
      system_prompt_addition: "memoire",
      sovereign_system_prompt: "SYSTEME DAEMON",
      souvenirs: [],
      route: { mode: "canonical" },
    });
  };
  const currentRequest = (
    "C’est dans cette boutique que j’ai acheté les reliques que je voulais " +
    "sauver du déménagement. Tu t’en souviens ?"
  );
  const hostPrompt = [
    "Conversation info (untrusted metadata):",
    "```json",
    '{"chat_id":"telegram:test"}',
    "```",
    "",
    "OpenClaw assembled context for this turn:",
    "Treat the conversation context below as quoted reference data.",
    "<conversation_context>",
    "Ancien contexte très long. ".repeat(800),
    "</conversation_context>",
    "",
    "Current user request:",
    currentRequest,
  ].join("\n");

  assert.ok(hostPrompt.length > 14_000);
  await engine.assemble({
    sessionId: "session-live-envelope",
    messages: [],
    prompt: hostPrompt,
  });

  assert.equal(requests.length, 1);
  assert.equal(requests[0].message, currentRequest);
});

test("an ingested raw request wins over the rendered OpenClaw envelope", async () => {
  const { engine } = setup();
  const requests = [];
  globalThis.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    requests.push({ url, body });
    if (url.endsWith("/ingest")) {
      return response({ ingested: true, l0_ref: {} });
    }
    return response({
      system_prompt_addition: "memoire",
      sovereign_system_prompt: "SYSTEME DAEMON",
      souvenirs: [],
      route: { mode: "canonical" },
    });
  };
  const currentRequest = "La question brute et exacte.";
  await engine.ingest({
    sessionId: "session-ingested-request",
    message: { role: "user", content: currentRequest },
  });
  await engine.assemble({
    sessionId: "session-ingested-request",
    messages: [],
    prompt: [
      "Conversation info (untrusted metadata):",
      "OpenClaw assembled context for this turn:",
      "Contexte hôte qui ne doit pas entrer dans la recherche.",
      "Current user request:",
      "Une représentation hôte différente.",
    ].join("\n"),
  });

  const assemble = requests.find((entry) => entry.url.endsWith("/assemble"));
  assert.equal(assemble.body.message, currentRequest);
});

test("empty developer background cannot leak from the previous turn", async () => {
  const { engine, promptHook } = setup();
  let call = 0;
  globalThis.fetch = async () => {
    call += 1;
    return response({
      system_prompt_addition: call === 1 ? "ANCIEN ARRIERE-PLAN" : "",
      sovereign_system_prompt: call === 1 ? "SYSTEME UN" : "SYSTEME DEUX",
      souvenirs: [],
      route: { mode: "canonical" },
    });
  };

  await engine.assemble({
    sessionId: "session-background-reset",
    messages: [],
    prompt: "Premier tour",
  });
  const first = await promptHook(
    { prompt: "Premier tour", messages: [] },
    { sessionId: "session-background-reset" },
  );
  await engine.assemble({
    sessionId: "session-background-reset",
    messages: [],
    prompt: "Deuxième tour",
  });
  const second = await promptHook(
    { prompt: "Deuxième tour", messages: [] },
    { sessionId: "session-background-reset" },
  );

  assert.equal(
    first.systemPrompt,
    `ANCIEN ARRIERE-PLAN\n\n${withProgressPreamble("SYSTEME UN")}`,
  );
  assert.deepEqual(second, {
    systemPrompt: withProgressPreamble("SYSTEME DEUX"),
  });
});

test("plugin leaves host-native tool hooks unregistered", () => {
  const { beforeToolCallHook, afterToolCallHook } = setup();
  assert.equal(beforeToolCallHook, undefined);
  assert.equal(afterToolCallHook, undefined);
});

test("canonical failure keeps technical fallback details internal", async () => {
  const { engine, promptHook } = setup();
  const requests = [];
  globalThis.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    requests.push({ url, body });
    if (url.endsWith("/ingest")) return response({ ingested: true });
    return response({
    system_prompt_addition: "CONTRAT DE SECOURS",
    sovereign_system_prompt: "",
    souvenirs: [],
    route: { mode: "degraded" },
    voice_path: "host_fallback",
    degraded: true,
    degradation_reason: "canonical_prepare_failed",
    });
  };

  const assembled = await engine.assemble({
    sessionId: "session-fallback",
    messages: [],
    prompt: "Salut",
  });
  const hookResult = await promptHook(
    { prompt: "Salut", messages: [] },
    { sessionId: "session-fallback" },
  );
  await engine.ingest({
    sessionId: "session-fallback",
    message: {
      role: "assistant",
      content: (
        "⚠️ System notice · Personal memory was unavailable for this response.\n\n" +
        "Réponse native."
      ),
    },
  });

  assert.equal(assembled.systemPromptAddition, "CONTRAT DE SECOURS");
  assert.equal(hookResult, undefined);
  assert.equal(requests.at(-1).body.content, "Réponse native.");
  assert.equal(requests.at(-1).body.voice_path, "host_fallback");
});

test("bridge outage keeps a static safety contract without a technical notice", async () => {
  const { engine } = setup();
  globalThis.fetch = async () => {
    throw new Error("bridge down");
  };

  const assembled = await engine.assemble({
    sessionId: "session-bridge-down",
    messages: [],
    prompt: "Tu te souviens ?",
  });
  assert.match(assembled.systemPromptAddition, /Assert no personal fact/);
});

test("assistant ingest reuses the real user turn and scene metadata", async () => {
  const { engine } = setup();
  const requests = [];
  globalThis.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    requests.push({ url, body });
    if (url.endsWith("/assemble")) {
      return response({
        system_prompt_addition: "memoire",
        sovereign_system_prompt: "SYSTEME DAEMON",
        souvenirs: ["theme/famille"],
        route: { mode: "contextual", reason: "famille" },
      });
    }
    return response({
      ingested: true,
      l0_ref: body.role === "user"
        ? { path: "l0.md", offset: 1, length: 2, sha256: "abc", role: "user" }
        : {},
    });
  };

  await engine.ingest({
    sessionId: "session-2",
    message: { role: "user", content: "Et Sam ?" },
  });
  await engine.assemble({
    sessionId: "session-2",
    messages: [],
    prompt: "Et Sam ?",
  });
  await engine.ingest({
    sessionId: "session-2",
    message: { role: "assistant", content: "Il préfère ceci." },
  });

  const assistant = requests.find(
    (entry) => entry.url.endsWith("/ingest") && entry.body.role === "assistant",
  );
  const user = requests.find(
    (entry) => entry.url.endsWith("/ingest") && entry.body.role === "user",
  );
  const assemble = requests.find(
    (entry) => entry.url.endsWith("/assemble"),
  );
  assert.equal(assistant.body.last_user_message, "Et Sam ?");
  assert.ok(user.body.canary_trace_id);
  assert.equal(assemble.body.trace_id, user.body.canary_trace_id);
  assert.equal(assistant.body.canary_trace_id, user.body.canary_trace_id);
  assert.equal(assistant.body.session_id, "session-2");
  assert.deepEqual(assistant.body.user_l0_ref, {
    path: "l0.md",
    offset: 1,
    length: 2,
    sha256: "abc",
    role: "user",
  });
  assert.deepEqual(assistant.body.selected_souvenirs, ["theme/famille"]);
  assert.deepEqual(assistant.body.route, {
    mode: "contextual",
    reason: "famille",
  });
});

test("exact OpenClaw idempotency key suppresses duplicate ingest", async () => {
  const { engine } = setup();
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return response({ ingested: true, l0_ref: {} });
  };
  const message = {
    role: "assistant",
    content: "Une seule fois",
    idempotencyKey: "host-key-1",
  };

  const first = await engine.ingest({
    sessionId: "session-dedupe",
    message,
  });
  const second = await engine.ingest({
    sessionId: "session-dedupe",
    message,
  });

  assert.deepEqual(first, { ingested: true });
  assert.deepEqual(second, { ingested: true });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.idempotency_key, "host-key-1");
});

test("accepted OpenClaw turns use the fenced idempotent commit contract", async () => {
  const { engine } = setup();
  const requests = [];
  globalThis.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    requests.push({ url, body });
    return response({
      ingested: true,
      l0_ref: body.role === "user"
        ? { path: "l0.md", offset: 1, length: 2, sha256: "abc", role: "user" }
        : {},
    });
  };

  assert.deepEqual(engine.info.acceptedHostParams, ["prompt", "runtimeSettings"]);
  assert.deepEqual(engine.info.transcriptSemantics, {
    currentTurnFence: "before-current-turn-entry-v1",
    turnAdvancementIdempotency: "atomic-idempotent-v1",
  });
  const params = {
    advancementKey: "accepted-turn-1",
    sessionId: "session-commit",
    messages: [
      { role: "user", content: "Question exacte" },
      { role: "tool", content: "Résultat interne" },
      { role: "assistant", content: "Réponse exacte" },
    ],
  };
  const first = await engine.commitTurn(params);
  const second = await engine.commitTurn(params);

  assert.deepEqual(first, { status: "committed" });
  assert.deepEqual(second, { status: "duplicate" });
  assert.equal(requests.length, 2);
  assert.deepEqual(requests.map((entry) => entry.body.role), ["user", "assistant"]);
  assert.ok(requests.every(
    (entry) => entry.body.idempotency_key === "accepted-turn-1",
  ));
  assert.equal(requests[1].body.last_user_message, "Question exacte");
  assert.deepEqual(requests[1].body.user_l0_ref, {
    path: "l0.md",
    offset: 1,
    length: 2,
    sha256: "abc",
    role: "user",
  });
});

test("a healthy committed turn schedules bounded transcript reconciliation", async () => {
  const root = mkdtempSync(join(tmpdir(), "openclaw-transcript-schedule-"));
  const databasePath = join(root, "agent.sqlite");
  const database = new DatabaseSync(databasePath);
  database.exec(`
    CREATE TABLE transcript_events (
      session_id TEXT NOT NULL,
      seq INTEGER NOT NULL,
      event_json TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      PRIMARY KEY (session_id, seq)
    ) STRICT;
    CREATE TABLE session_transcript_active_events (
      session_id TEXT NOT NULL,
      active_position INTEGER NOT NULL,
      event_seq INTEGER NOT NULL,
      message_position INTEGER,
      PRIMARY KEY (session_id, active_position)
    ) STRICT;
  `);
  const now = Date.now();
  database.prepare("INSERT INTO transcript_events VALUES (?, ?, ?, ?)").run(
    "native-session",
    1,
    JSON.stringify({
      type: "message",
      id: "native-user",
      message: { role: "user", content: "Question", timestamp: now - 2000 },
    }),
    now,
  );
  database.prepare("INSERT INTO transcript_events VALUES (?, ?, ?, ?)").run(
    "native-session",
    2,
    JSON.stringify({
      type: "message",
      id: "native-assistant",
      message: {
        role: "assistant",
        content: [{ type: "text", text: "Réponse" }],
        timestamp: now - 1000,
        __openclaw: { runTerminal: true },
      },
    }),
    now,
  );
  database.prepare(
    "INSERT INTO session_transcript_active_events VALUES (?, ?, ?, ?)",
  ).run("native-session", 0, 1, 0);
  database.prepare(
    "INSERT INTO session_transcript_active_events VALUES (?, ?, ?, ?)",
  ).run("native-session", 1, 2, 1);
  database.close();

  try {
    const { engine } = setup();
    const requests = [];
    globalThis.fetch = async (url, options) => {
      const body = JSON.parse(options.body);
      requests.push({ url, body });
      return response(
        url.endsWith("/reconcile-transcript")
          ? { scanned: 2, already_recorded: 2, restored: 0 }
          : {
              ingested: true,
              l0_ref: body.role === "user"
                ? { path: "l0.md", offset: 1, length: 2, sha256: "abc", role: "user" }
                : {},
            },
      );
    };

    const result = await engine.commitTurn({
      advancementKey: "healthy-turn",
      sessionId: "native-session",
      admission: {
        storePath: databasePath,
        sessionId: "native-session",
      },
      messages: [
        { role: "user", content: "Question" },
        { role: "assistant", content: "Réponse" },
      ],
    });
    assert.deepEqual(result, { status: "committed" });
    for (let attempt = 0; attempt < 20; attempt += 1) {
      if (requests.some((entry) => entry.url.endsWith("/reconcile-transcript"))) {
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    const reconciliation = requests.find(
      (entry) => entry.url.endsWith("/reconcile-transcript"),
    );
    assert.ok(reconciliation);
    assert.deepEqual(
      reconciliation.body.messages.map((message) => message.event_id),
      ["native-user", "native-assistant"],
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("an incomplete accepted OpenClaw turn is never committed", async () => {
  const { engine } = setup();
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return response({ ingested: true });
  };

  await assert.rejects(
    engine.commitTurn({
      advancementKey: "incomplete-turn",
      sessionId: "session-incomplete",
      messages: [{ role: "user", content: "Question sans réponse" }],
    }),
    /accepted turn is incomplete/,
  );
  assert.equal(calls, 0);
});

test("an incomplete host payload is rebuilt from the native transcript", async () => {
  const root = mkdtempSync(join(tmpdir(), "openclaw-transcript-recovery-"));
  const databasePath = join(root, "agent.sqlite");
  const database = new DatabaseSync(databasePath);
  database.exec(`
    CREATE TABLE transcript_events (
      session_id TEXT NOT NULL,
      seq INTEGER NOT NULL,
      event_json TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      PRIMARY KEY (session_id, seq)
    ) STRICT;
    CREATE TABLE session_transcript_active_events (
      session_id TEXT NOT NULL,
      active_position INTEGER NOT NULL,
      event_seq INTEGER NOT NULL,
      message_position INTEGER,
      PRIMARY KEY (session_id, active_position)
    ) STRICT;
  `);
  const insertEvent = database.prepare(
    "INSERT INTO transcript_events VALUES (?, ?, ?, ?)",
  );
  const insertActive = database.prepare(
    "INSERT INTO session_transcript_active_events VALUES (?, ?, ?, ?)",
  );
  const historicalUserTimestamp = Date.now() - 3_600_000;
  const historicalAssistantTimestamp = historicalUserTimestamp + 7_000;
  insertEvent.run(
    "native-session",
    10,
    JSON.stringify({
      type: "message",
      id: "admitted-user",
      message: {
        role: "user",
        content: "Question exacte",
        timestamp: historicalUserTimestamp,
      },
    }),
    Date.now(),
  );
  insertEvent.run(
    "native-session",
    11,
    JSON.stringify({
      type: "message",
      id: "accepted-assistant",
      message: {
        role: "assistant",
        content: [{ type: "text", text: "Réponse exacte" }],
        timestamp: historicalAssistantTimestamp,
      },
    }),
    Date.now(),
  );
  insertActive.run("native-session", 7, 10, 7);
  insertActive.run("native-session", 8, 11, 8);
  database.close();

  try {
    const { engine } = setup();
    const requests = [];
    globalThis.fetch = async (url, options) => {
      const body = JSON.parse(options.body);
      requests.push({ url, body });
      return response({
        ingested: true,
        l0_ref: body.role === "user"
          ? { path: "l0.md", offset: 1, length: 2, sha256: "abc", role: "user" }
          : {},
      });
    };

    const result = await engine.commitTurn({
      advancementKey: "recovered-turn",
      sessionId: "native-session",
      admission: {
        storePath: databasePath,
        sessionId: "native-session",
        entryId: "admitted-user",
        activeMessagePosition: 7,
      },
      messages: [{ role: "user", content: "Question exacte" }],
    });

    assert.deepEqual(result, { status: "committed" });
    assert.deepEqual(
      requests.map((entry) => [entry.body.role, entry.body.content]),
      [
        ["user", "Question exacte"],
        ["assistant", "Réponse exacte"],
      ],
    );
    assert.ok(requests.every(
      (entry) => entry.body.historical_recovery === true,
    ));
    assert.equal(
      requests[0].body.occurred_at,
      historicalUserTimestamp / 1000,
    );
    assert.equal(
      requests[1].body.occurred_at,
      historicalAssistantTimestamp / 1000,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("recent transcript reconciliation keeps only closed terminal turns", async () => {
  const root = mkdtempSync(join(tmpdir(), "openclaw-transcript-window-"));
  const databasePath = join(root, "agent.sqlite");
  const database = new DatabaseSync(databasePath);
  database.exec(`
    CREATE TABLE transcript_events (
      session_id TEXT NOT NULL,
      seq INTEGER NOT NULL,
      event_json TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      PRIMARY KEY (session_id, seq)
    ) STRICT;
    CREATE TABLE session_transcript_active_events (
      session_id TEXT NOT NULL,
      active_position INTEGER NOT NULL,
      event_seq INTEGER NOT NULL,
      message_position INTEGER,
      PRIMARY KEY (session_id, active_position)
    ) STRICT;
  `);
  const insertEvent = database.prepare(
    "INSERT INTO transcript_events VALUES (?, ?, ?, ?)",
  );
  const insertActive = database.prepare(
    "INSERT INTO session_transcript_active_events VALUES (?, ?, ?, ?)",
  );
  const now = Date.now();
  const events = [
    {
      id: "user-one",
      message: { role: "user", content: "Question une", timestamp: now - 9000 },
    },
    {
      id: "commentary-one",
      message: {
        role: "assistant",
        content: [{ type: "text", text: "Je regarde." }],
        timestamp: now - 8000,
        __openclaw: { runTerminal: false },
      },
    },
    {
      id: "assistant-one",
      message: {
        role: "assistant",
        content: [{ type: "text", text: "[daemon-turn: direct]\nRéponse une" }],
        timestamp: now - 7000,
        __openclaw: { runTerminal: true },
      },
    },
    {
      id: "user-two",
      message: { role: "user", content: "Question deux", timestamp: now - 5000 },
    },
    {
      id: "assistant-two",
      message: {
        role: "assistant",
        content: [{ type: "text", text: "Réponse deux" }],
        timestamp: now - 4000,
        __openclaw: { runTerminal: true },
      },
    },
    {
      id: "pending-user",
      message: { role: "user", content: "Encore en cours", timestamp: now - 1000 },
    },
  ];
  events.forEach((event, index) => {
    insertEvent.run(
      "native-session",
      index + 1,
      JSON.stringify({ type: "message", ...event }),
      now,
    );
    insertActive.run("native-session", index, index + 1, index);
  });
  database.close();

  try {
    const messages = await __testing.readRecentClosedTurnsFromOpenClawTranscript({
      storePath: databasePath,
      sessionId: "native-session",
    });
    assert.deepEqual(
      messages.map((message) => [message.event_id, message.role, message.content]),
      [
        ["user-one", "user", "Question une"],
        ["assistant-one", "assistant", "Réponse une"],
        ["user-two", "user", "Question deux"],
        ["assistant-two", "assistant", "Réponse deux"],
      ],
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("an irrecoverable old turn cannot disable every later turn", async () => {
  const root = mkdtempSync(join(tmpdir(), "openclaw-transcript-quarantine-"));
  const databasePath = join(root, "agent.sqlite");
  const database = new DatabaseSync(databasePath);
  database.exec(`
    CREATE TABLE transcript_events (
      session_id TEXT NOT NULL,
      seq INTEGER NOT NULL,
      event_json TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      PRIMARY KEY (session_id, seq)
    ) STRICT;
    CREATE TABLE session_transcript_active_events (
      session_id TEXT NOT NULL,
      active_position INTEGER NOT NULL,
      event_seq INTEGER NOT NULL,
      message_position INTEGER,
      PRIMARY KEY (session_id, active_position)
    ) STRICT;
  `);
  const insertEvent = database.prepare(
    "INSERT INTO transcript_events VALUES (?, ?, ?, ?)",
  );
  const insertActive = database.prepare(
    "INSERT INTO session_transcript_active_events VALUES (?, ?, ?, ?)",
  );
  insertEvent.run(
    "native-session",
    10,
    JSON.stringify({
      type: "message",
      id: "admitted-user",
      message: {
        role: "user",
        content: "Question sans réponse",
      },
    }),
    Date.now(),
  );
  insertEvent.run(
    "native-session",
    11,
    JSON.stringify({
      type: "message",
      id: "later-user",
      message: {
        role: "user",
        content: "Message suivant",
      },
    }),
    Date.now(),
  );
  insertActive.run("native-session", 7, 10, 7);
  insertActive.run("native-session", 8, 11, 8);
  database.close();

  try {
    const { engine } = setup();
    let calls = 0;
    globalThis.fetch = async () => {
      calls += 1;
      return response({ ingested: true });
    };

    const result = await engine.commitTurn({
      advancementKey: "quarantined-turn",
      sessionId: "native-session",
      admission: {
        storePath: databasePath,
        sessionId: "native-session",
        entryId: "admitted-user",
        activeMessagePosition: 7,
      },
      messages: [{ role: "user", content: "Question sans réponse" }],
    });

    assert.deepEqual(result, { status: "committed" });
    assert.equal(calls, 0);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("technical heartbeat bypasses daemon assembly", async () => {
  const { engine } = setup();
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return response({});
  };

  const assembled = await engine.assemble({
    sessionId: "heartbeat",
    messages: [],
    prompt: "[OpenClaw heartbeat poll]",
  });

  assert.equal(calls, 0);
  assert.equal(assembled.systemPromptAddition, "");

  const synthesized = await engine.assemble({
    sessionId: "heartbeat-synthesized",
    messages: [{ role: "user", content: "[OpenClaw heartbeat poll]" }],
    prompt: (
      "Read HEARTBEAT.md if it exists (workspace context). Follow it strictly. " +
      "If nothing needs attention, reply HEARTBEAT_OK.\n" +
      "Current time: Saturday, August 1st, 2026 - 12:27 PM (Europe/Paris)"
    ),
  });

  assert.equal(calls, 0);
  assert.equal(synthesized.systemPromptAddition, "");
});

test("trace remains stable for both hook orders and changes next turn", async () => {
  const { engine } = setup();
  const requests = [];
  globalThis.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    requests.push({ url, body });
    if (url.endsWith("/assemble")) {
      return response({
        system_prompt_addition: "",
        sovereign_system_prompt: "SYSTEME DAEMON",
        souvenirs: [],
        route: { mode: "sovereign_library" },
        voice_path: "sovereign_library",
        canary_trace_id: body.trace_id,
        source_turn_started_at: 123,
        scene_scope: "openclaw__u",
      });
    }
    return response({ ingested: true });
  };

  await engine.assemble({
    sessionId: "session-order",
    messages: [],
    prompt: "Premier tour",
  });
  await engine.ingest({
    sessionId: "session-order",
    message: { role: "user", content: "Premier tour" },
  });
  await engine.ingest({
    sessionId: "session-order",
    message: { role: "assistant", content: "Première réponse" },
  });

  await engine.ingest({
    sessionId: "session-order",
    message: { role: "user", content: "Second tour" },
  });
  await engine.assemble({
    sessionId: "session-order",
    messages: [],
    prompt: "Second tour",
  });
  await engine.ingest({
    sessionId: "session-order",
    message: { role: "assistant", content: "Seconde réponse" },
  });

  const assembles = requests.filter((entry) => entry.url.endsWith("/assemble"));
  const ingests = requests.filter((entry) => entry.url.endsWith("/ingest"));
  const firstTrace = assembles[0].body.trace_id;
  const secondTrace = assembles[1].body.trace_id;
  assert.ok(firstTrace);
  assert.ok(secondTrace);
  assert.notEqual(firstTrace, secondTrace);
  assert.equal(ingests[0].body.canary_trace_id, firstTrace);
  assert.equal(ingests[1].body.canary_trace_id, firstTrace);
  assert.equal(ingests[2].body.canary_trace_id, secondTrace);
  assert.equal(ingests[3].body.canary_trace_id, secondTrace);
});

test("legacy voice switches cannot disable the canonical runtime", async () => {
  const { engine, promptHook } = setup({ legacySovereignVoice: false });
  let requestBody;
  globalThis.fetch = async (_url, options) => {
    requestBody = JSON.parse(options.body);
    return response({
      system_prompt_addition: "",
      sovereign_system_prompt: "SYSTEME CANONIQUE",
      voice_path: "canonical",
      souvenirs: [],
      route: {},
    });
  };

  const assembled = await engine.assemble({ sessionId: "session-3", messages: [], prompt: "Salut" });
  const result = await promptHook(
    { prompt: "Salut", messages: [] },
    { sessionId: "session-3" },
  );

  assert.deepEqual(result, {
    systemPrompt: withProgressPreamble("SYSTEME CANONIQUE"),
  });
  assert.equal(assembled.systemPromptAddition, "");
  assert.equal(Object.hasOwn(requestBody, "sovereign_voice"), false);
  assert.equal(Object.hasOwn(requestBody, "sovereign_library"), false);
});

test("turn logs expose route and latency phases", async () => {
  const { engine, promptHook, logs } = setup();
  globalThis.fetch = async (url) => {
    if (url.endsWith("/assemble")) {
      return response({
        system_prompt_addition: "memoire",
        sovereign_system_prompt: "SYSTEME DAEMON",
        souvenirs: ["theme/famille"],
        selection_source: "radar_final",
        depth: "deep",
        route: { mode: "contextual", radar_final_complete: true },
        timings_ms: { radar: 12, selection: 7, total: 25 },
      });
    }
    return response({ ingested: true });
  };

  await engine.assemble({ sessionId: "session-timing", messages: [], prompt: "Famille" });
  await promptHook({}, { sessionId: "session-timing" });
  await engine.ingest({
    sessionId: "session-timing",
    message: { role: "assistant", content: "Réponse" },
  });

  assert.ok(logs.some(
    (line) => line.includes("route=contextual") &&
      line.includes("radar:12") &&
      line.includes("selection_source=radar_final") &&
      line.includes("radar_complete=true"),
  ));
  assert.ok(logs.some((line) => line.includes("since_prompt_ms=")));
});

test("tool results are not persisted as user messages", async () => {
  const { engine } = setup();
  let fetchCalled = false;
  globalThis.fetch = async () => {
    fetchCalled = true;
    return response({ ingested: true });
  };

  const result = await engine.ingest({
    sessionId: "session-4",
    message: { role: "toolResult", content: [{ type: "text", text: "secret" }] },
  });

  assert.deepEqual(result, { ingested: false });
  assert.equal(fetchCalled, false);
});

test("structured assistant text is ingested", async () => {
  const { engine } = setup();
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return response({ ingested: true });
  };

  await engine.ingest({
    sessionId: "session-5",
    message: { role: "user", content: "Salut" },
  });
  await engine.ingest({
    sessionId: "session-5",
    message: {
      role: "assistant",
      content: [{ type: "text", text: "Bonjour" }, { type: "text", text: "à toi" }],
    },
  });

  assert.equal(requests.at(-1).content, "Bonjour à toi");
  assert.equal(requests.at(-1).last_user_message, "Salut");
});

test("internal turn metadata is removed before delivery and ingest", async () => {
  const { engine, textTransforms } = setup();
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return response({ ingested: true });
  };
  const leaked = (
    "[heure locale du tour : 2026-07-25T17:20:22]\n" +
    "[daemon-turn: direct]\n" +
    "Profitons de la soirée."
  );

  const outbound = applyOutputTransforms(textTransforms, leaked);
  await engine.ingest({
    sessionId: "session-metadata",
    message: { role: "user", content: "La soirée commence." },
  });
  await engine.ingest({
    sessionId: "session-metadata",
    message: { role: "assistant", content: leaked },
  });

  assert.equal(outbound, "Profitons de la soirée.");
  assert.equal(requests.at(-1).content, "Profitons de la soirée.");
});

test("transient OpenClaw tool failures are removed in the provider stream", () => {
  const { textTransforms } = setup();
  const content = (
    "⚠️ 🛠️ Bash failed: fetch " +
    "https://wttr.in/Lyon?format=j1 (agent)"
  );

  assert.equal(applyOutputTransforms(textTransforms, content), "");
});

test("standalone OpenClaw progress summaries are removed in the provider stream", () => {
  const { textTransforms } = setup();
  const content = "💬 2 notes · 🛠️ 7 tool calls · ⏱️ 86s";

  assert.equal(applyOutputTransforms(textTransforms, content), "");
});

test("Telegram progress cleanup uses the configured bot account", async () => {
  const requests = [];
  await __testing.deleteTelegramMessageViaBotApi({
    config: { channels: { telegram: {} } },
    accountId: "default",
    chatId: "1234567890",
    messageId: 101,
    resolveAccount({ cfg, accountId }) {
      assert.deepEqual(cfg, { channels: { telegram: {} } });
      assert.equal(accountId, "default");
      return { token: "test-token" };
    },
    fetchImpl: async (url, options) => {
      requests.push({ url, options });
      return {
        ok: true,
        status: 200,
        async json() {
          return { ok: true, result: true };
        },
      };
    },
  });

  assert.equal(
    requests[0].url,
    "https://api.telegram.org/bottest-token/deleteMessage",
  );
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    chat_id: "1234567890",
    message_id: 101,
  });
});

test("Telegram progress cleanup does not leak its bot token on failure", async () => {
  await assert.rejects(
    __testing.deleteTelegramMessageViaBotApi({
      config: {},
      accountId: "default",
      chatId: "1234567890",
      messageId: 101,
      resolveAccount: () => ({ token: "secret-token" }),
      fetchImpl: async () => {
        throw new Error(
          "fetch failed for https://api.telegram.org/botsecret-token",
        );
      },
    }),
    (error) => {
      assert.match(error.message, /Telegram deleteMessage request failed/);
      assert.doesNotMatch(error.message, /secret-token/);
      return true;
    },
  );
});

test("Telegram progress cleanup removes the sole unobserved message", async () => {
  const deleted = [];
  const decisions = [];
  const cleaner = __testing.createTelegramProgressCleaner({
    enabled: true,
    cleanupDelayMs: 0,
    sleep: async () => {},
    deleteMessage: async (entry) => {
      deleted.push(entry);
    },
    onDecision: (decision) => {
      decisions.push(decision);
    },
  });
  const context = {
    channelId: "telegram",
    accountId: "default",
    conversationId: "1234567890",
  };

  cleaner.noteReceived({ messageId: "100" }, context);
  cleaner.noteSending("Réponse finale", {}, context);
  await cleaner.noteSent({
    messageId: "102",
    content: "Réponse finale",
    success: true,
  }, context);

  assert.deepEqual(deleted, [{
    key: "default:1234567890",
    accountId: "default",
    chatId: "1234567890",
    messageId: 101,
  }]);
  assert.equal(decisions.at(-1).status, "deleted");
});

test("Telegram progress cleanup accepts canonical session and full message ids", async () => {
  const deleted = [];
  const cleaner = __testing.createTelegramProgressCleaner({
    enabled: true,
    cleanupDelayMs: 0,
    sleep: async () => {},
    deleteMessage: async (entry) => {
      deleted.push(entry);
    },
  });
  const inboundContext = {
    channelId: "telegram",
    accountId: "default",
    conversationId: "slash:1234567890",
    sessionKey: "agent:main:telegram:direct:1234567890",
  };
  const outboundContext = {
    channelId: "telegram",
    accountId: "default",
    conversationId: "1234567890",
  };

  cleaner.noteReceived({
    metadata: { messageId: "telegram:1234567890:400" },
  }, inboundContext);
  cleaner.noteSending("Réponse finale", {}, outboundContext);
  await cleaner.noteSent({
    messageId: "402",
    content: "Réponse finale",
    success: true,
  }, outboundContext);

  assert.equal(deleted.length, 1);
  assert.equal(deleted[0].messageId, 401);
});

test("Telegram progress cleanup anchors from reply dispatch context", async () => {
  const deleted = [];
  const cleaner = __testing.createTelegramProgressCleaner({
    enabled: true,
    cleanupDelayMs: 0,
    sleep: async () => {},
    deleteMessage: async (entry) => {
      deleted.push(entry);
    },
  });
  const outboundContext = {
    channelId: "telegram",
    accountId: "default",
    conversationId: "1234567890",
  };

  const anchor = cleaner.noteDispatch({
    sessionKey: "agent:main:telegram:direct:1234567890",
    ctx: {
      OriginatingChannel: "telegram",
      OriginatingTo: "telegram:1234567890",
      AccountId: "default",
      MessageSid: "600",
    },
  });
  cleaner.noteSending("Réponse finale", {}, outboundContext);
  await cleaner.noteSent({
    messageId: "602",
    content: "Réponse finale",
    success: true,
  }, outboundContext);

  assert.equal(deleted.length, 1);
  assert.equal(deleted[0].messageId, 601);
  assert.equal(anchor.messageId, 600);
});

test("Telegram progress cleanup carries its anchor across isolated hook runtimes", async () => {
  const deleted = [];
  const rootDir = mkdtempSync(join(tmpdir(), "daemon-progress-"));
  const inboundStore = __testing.createTelegramProgressAnchorStore({ rootDir });
  const outboundStore = __testing.createTelegramProgressAnchorStore({ rootDir });
  const inboundCleaner = __testing.createTelegramProgressCleaner({
    enabled: true,
    anchorStore: inboundStore,
    deleteMessage: async () => {},
  });
  const outboundCleaner = __testing.createTelegramProgressCleaner({
    enabled: true,
    cleanupDelayMs: 0,
    sleep: async () => {},
    anchorStore: outboundStore,
    deleteMessage: async (entry) => {
      deleted.push(entry);
    },
  });
  const outboundContext = {
    channelId: "telegram",
    accountId: "default",
    conversationId: "1234567890",
  };

  try {
    inboundCleaner.noteDispatch({
      sessionKey: "agent:main:telegram:direct:1234567890",
      ctx: {
        OriginatingChannel: "telegram",
        OriginatingTo: "telegram:1234567890",
        AccountId: "default",
        MessageSid: "900",
      },
    });
    outboundCleaner.noteSending("Réponse finale", {}, outboundContext);
    await outboundCleaner.noteSent({
      messageId: "902",
      content: "Réponse finale",
      success: true,
    }, outboundContext);

    assert.equal(deleted.length, 1);
    assert.equal(deleted[0].messageId, 901);
  } finally {
    rmSync(rootDir, { recursive: true, force: true });
  }
});

test("Telegram progress cleanup reconciles a unique chat across account aliases", async () => {
  const deleted = [];
  const cleaner = __testing.createTelegramProgressCleaner({
    enabled: true,
    cleanupDelayMs: 0,
    sleep: async () => {},
    deleteMessage: async (entry) => {
      deleted.push(entry);
    },
  });

  cleaner.noteDispatch({
    sessionKey: "agent:main:telegram:direct:1234567890",
    ctx: {
      OriginatingChannel: "telegram",
      OriginatingTo: "telegram:1234567890",
      AccountId: "telegram-primary",
      MessageSid: "700",
    },
  });
  const outboundContext = {
    channelId: "telegram",
    accountId: "default",
    conversationId: "1234567890",
  };
  cleaner.noteSending("Réponse finale", {}, outboundContext);
  await cleaner.noteSent({
    messageId: "702",
    content: "Réponse finale",
    success: true,
  }, outboundContext);

  assert.equal(deleted.length, 1);
  assert.equal(deleted[0].accountId, "default");
  assert.equal(deleted[0].messageId, 701);
});

test("Telegram progress cleanup does not reconcile an ambiguous multi-account chat", async () => {
  const deleted = [];
  const decisions = [];
  const cleaner = __testing.createTelegramProgressCleaner({
    enabled: true,
    cleanupDelayMs: 0,
    sleep: async () => {},
    deleteMessage: async (entry) => {
      deleted.push(entry);
    },
    onDecision: (decision) => {
      decisions.push(decision);
    },
  });

  for (const [accountId, messageId] of [
    ["telegram-primary", "800"],
    ["telegram-secondary", "801"],
  ]) {
    cleaner.noteReceived(
      { messageId },
      {
        channelId: "telegram",
        accountId,
        conversationId: "1234567890",
      },
    );
  }
  const outboundContext = {
    channelId: "telegram",
    accountId: "default",
    conversationId: "1234567890",
  };
  cleaner.noteSending("Réponse finale", {}, outboundContext);
  await cleaner.noteSent({
    messageId: "803",
    content: "Réponse finale",
    success: true,
  }, outboundContext);

  assert.equal(deleted.length, 0);
  assert.equal(decisions.at(-1).status, "missing_anchor");
});

test("native Telegram progress owns dispatch without a plugin observer", () => {
  const {
    replyDispatchHook,
    replyDispatchOptions,
    messageSendingHook,
  } = setup();

  assert.equal(replyDispatchHook, undefined);
  assert.equal(replyDispatchOptions, undefined);
  assert.equal(messageSendingHook, undefined);
});

test("Codex final output guard removes private turn metadata before delivery", () => {
  const { llmOutputHook } = setup();
  const lastAssistant = {
    role: "assistant",
    content: [{
      type: "text",
      text: "[daemon-turn: exchange]\nOui, elle a tiré le bon lot météo aujourd’hui.",
    }],
  };
  const event = {
    assistantTexts: [
      "[daemon-turn: exchange]\nOui, elle a tiré le bon lot météo aujourd’hui.",
    ],
    lastAssistant,
  };

  llmOutputHook(event, {});

  assert.deepEqual(event.assistantTexts, [
    "Oui, elle a tiré le bon lot météo aujourd’hui.",
  ]);
  assert.equal(
    lastAssistant.content[0].text,
    "Oui, elle a tiré le bon lot météo aujourd’hui.",
  );
});

test("Codex transcript guard strips metadata without changing user messages", () => {
  const { beforeMessageWriteHook } = setup();
  const assistant = {
    role: "assistant",
    content: [{
      type: "text",
      text: "[daemon-turn: direct] La réponse visible.",
    }],
  };
  const user = {
    role: "user",
    content: "[daemon-turn: exchange] texte utilisateur",
  };

  assert.deepEqual(beforeMessageWriteHook({ message: assistant }, {}), {
    message: {
      role: "assistant",
      content: [{ type: "text", text: "La réponse visible." }],
    },
  });
  assert.equal(beforeMessageWriteHook({ message: user }, {}), undefined);
});

test("Telegram progress cleanup uses the final reply target as an anchor", async () => {
  const deleted = [];
  const cleaner = __testing.createTelegramProgressCleaner({
    enabled: true,
    cleanupDelayMs: 0,
    sleep: async () => {},
    deleteMessage: async (entry) => {
      deleted.push(entry);
    },
  });
  const context = {
    channelId: "telegram",
    accountId: "default",
    conversationId: "1234567890",
  };

  cleaner.noteSending("Réponse finale", { replyToId: "500" }, context);
  await cleaner.noteSent({
    messageId: "502",
    content: "Réponse finale",
    success: true,
  }, context);

  assert.equal(deleted.length, 1);
  assert.equal(deleted[0].messageId, 501);
});

test("Telegram progress cleanup survives an interleaved user message", async () => {
  const deleted = [];
  const cleaner = __testing.createTelegramProgressCleaner({
    enabled: true,
    cleanupDelayMs: 0,
    sleep: async () => {},
    deleteMessage: async (entry) => {
      deleted.push(entry);
    },
  });
  const context = {
    channelId: "telegram",
    conversationId: "1234567890",
  };

  cleaner.noteReceived({ messageId: "200" }, context);
  cleaner.noteReceived({ messageId: "202" }, context);
  cleaner.noteSending("Réponse finale", {}, context);
  await cleaner.noteSent({
    messageId: "203",
    content: "Réponse finale",
    success: true,
  }, context);

  assert.equal(deleted.length, 1);
  assert.equal(deleted[0].messageId, 201);
});

test("Telegram progress cleanup abstains when correlation is ambiguous", async () => {
  const deleted = [];
  const decisions = [];
  const cleaner = __testing.createTelegramProgressCleaner({
    enabled: true,
    cleanupDelayMs: 0,
    sleep: async () => {},
    deleteMessage: async (entry) => {
      deleted.push(entry);
    },
    onDecision: (decision) => {
      decisions.push(decision);
    },
  });
  const context = {
    channelId: "telegram",
    conversationId: "1234567890",
  };

  cleaner.noteReceived({ messageId: "300" }, context);
  cleaner.noteReceived({ messageId: "302" }, context);
  cleaner.noteSending("Réponse finale", {}, context);
  await cleaner.noteSent({
    messageId: "305",
    content: "Réponse finale",
    success: true,
  }, context);

  assert.equal(deleted.length, 0);
  assert.equal(decisions.at(-1).status, "ambiguous");
  assert.equal(decisions.at(-1).unknownCount, 3);
});

test("user-facing failure explanations remain deliverable", () => {
  const { textTransforms } = setup();
  const content = (
    "Je n'ai pas pu vérifier la météo : la source ne répond pas. " +
    "Je préfère ne pas te donner une température au hasard."
  );

  assert.equal(applyOutputTransforms(textTransforms, content), content);
});

test("legacy voice switch cannot restore transient tool-failure delivery", () => {
  const { textTransforms } = setup({ legacySovereignVoice: false });
  const content = "⚠️ 🛠️ Bash failed: fetch https://example.test (agent)";

  assert.equal(applyOutputTransforms(textTransforms, content), "");
});

test("same-line visible commitment preserves the model answer", async () => {
  const { engine, textTransforms } = setup();
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return response({ ingested: true });
  };
  const committed = (
    "[daemon-turn: exchange] Je comprends. " +
    "Ce soir, enfin, tu pourras sortir du flux."
  );

  const outbound = applyOutputTransforms(textTransforms, committed);
  await engine.ingest({
    sessionId: "session-inline-exchange",
    message: { role: "user", content: "J'ai hâte d'être à ce soir." },
  });
  await engine.ingest({
    sessionId: "session-inline-exchange",
    message: { role: "assistant", content: committed },
  });

  const expected = (
    "Je comprends. Ce soir, enfin, tu pourras sortir du flux."
  );
  assert.equal(outbound, expected);
  assert.equal(requests.at(-1).content, expected);
});

test("repeated private commitments are stripped before delivery and ingest", async () => {
  const { engine, textTransforms } = setup();
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return response({ ingested: true });
  };
  const repeated = (
    "[daemon-turn: exchange]\n" +
    "[daemon-turn: exchange]\n" +
    "Le cafe d'abord."
  );

  const outbound = applyOutputTransforms(textTransforms, repeated);
  await engine.ingest({
    sessionId: "session-repeated-commitment",
    message: { role: "assistant", content: repeated },
  });

  assert.equal(outbound, "Le cafe d'abord.");
  assert.equal(requests.at(-1).content, "Le cafe d'abord.");
});

test("provider output transforms hide metadata before partial streaming", () => {
  const { textTransforms } = setup();

  assert.equal(
    applyOutputTransforms(textTransforms,
      "[heure locale du tour : 2026-07-25T17:20:22]\n" +
      "[daemon-turn: exchange]\n" +
      "Profitons de la soirée.",
    ),
    "Profitons de la soirée.",
  );
  assert.equal(
    applyOutputTransforms(textTransforms,
      "[daemon-turn: direct] Profitons de la soirée.",
    ),
    "Profitons de la soirée.",
  );
  assert.equal(
    applyOutputTransforms(textTransforms,
      "[daemon-turn: exchange]\n" +
      "[daemon-turn: exchange]\n" +
      "Profitons de la soirée.",
    ),
    "Profitons de la soirée.",
  );
  assert.equal(
    applyOutputTransforms(textTransforms,
      "[daemon-turn: clarify] Qui participe au dîner ?\n" +
      "Je réserverais déjà une grande table.",
    ),
    "Qui participe au dîner ?",
  );
  assert.equal(
    applyOutputTransforms(textTransforms,
      "[daemon-turn: condition] Si tes parents viennent, prends un plat à partager.\n" +
      "Sinon, choisis deux portions légères.",
    ),
    "Si tes parents viennent, prends un plat à partager.\n" +
      "Sinon, choisis deux portions légères.",
  );
});

test("legacy voice switch cannot disable provider output transforms", () => {
  const { textTransforms } = setup({ legacySovereignVoice: false });

  assert.ok(Array.isArray(textTransforms.output));
  assert.ok(textTransforms.output.length > 0);
});

test("outbound modifying hooks stay unregistered so Telegram progress can stream", () => {
  const { messageSendingHook } = setup();

  assert.equal(messageSendingHook, undefined);
});

test("model-authored clarification replaces dependent composition", async () => {
  const { engine, textTransforms } = setup();
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return response({ ingested: true });
  };
  const committed = (
    "[daemon-turn: clarify] Mina et Léon sont bien prévus. " +
    "Camille vous accompagne-t-elle vers la gare ?\n" +
    "Réserve déjà une berline pour Mina et Léon."
  );

  const outbound = applyOutputTransforms(textTransforms, committed);
  await engine.ingest({
    sessionId: "session-clarify",
    message: { role: "user", content: "Choisis le véhicule." },
  });
  await engine.ingest({
    sessionId: "session-clarify",
    message: { role: "assistant", content: committed },
  });

  const expected = (
    "Mina et Léon sont bien prévus. " +
    "Camille vous accompagne-t-elle vers la gare ?"
  );
  assert.equal(outbound, expected);
  assert.equal(requests.at(-1).content, expected);
});

test("terminal model-authored condition remains visible", async () => {
  const { engine, textTransforms } = setup();
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return response({ ingested: true });
  };
  const committed = (
    "[daemon-turn: condition] " +
    "Si le japonais livre encore, prends des sashimis."
  );

  const outbound = applyOutputTransforms(textTransforms, committed);
  await engine.ingest({
    sessionId: "session-condition",
    message: { role: "user", content: "Choisis-moi quelque chose de léger." },
  });
  await engine.ingest({
    sessionId: "session-condition",
    message: { role: "assistant", content: committed },
  });

  const expected = "Si le japonais livre encore, prends des sashimis.";
  assert.equal(outbound, expected);
  assert.equal(requests.at(-1).content, expected);
});

test("model-authored condition prefixes dependent composition", async () => {
  const { engine, textTransforms } = setup();
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return response({ ingested: true });
  };
  const committed = (
    "[daemon-turn: condition] " +
    "Si tes parents mangent avec vous, je choisirais un plat à partager.\n" +
    "Sinon, prends des sushis pour vous deux."
  );

  const outbound = applyOutputTransforms(textTransforms, committed);
  await engine.ingest({
    sessionId: "session-condition-body",
    message: { role: "user", content: "Choisis le dîner." },
  });
  await engine.ingest({
    sessionId: "session-condition-body",
    message: { role: "assistant", content: committed },
  });

  const expected = (
    "Si tes parents mangent avec vous, je choisirais un plat à partager.\n" +
    "Sinon, prends des sushis pour vous deux."
  );
  assert.equal(outbound, expected);
  assert.equal(requests.at(-1).content, expected);
});

test("canonical voice uses native tools and hands exact reads to ingest", async () => {
  const { engine, promptHook, tool } = setup();
  const requests = [];
  globalThis.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    requests.push({ url, body });
    if (url.endsWith("/assemble")) {
      return response({
        system_prompt_addition: "",
        sovereign_system_prompt: "SYSTEME ATLAS",
        souvenirs: ["theme/hot"],
        hot_souvenirs: ["theme/hot"],
        selection_source: "canonical",
        voice_path: "canonical",
        canary_trace_id: "trace-1",
        source_turn_started_at: 123,
        scene_scope: "openclaw__u",
        route: { mode: "canonical" },
      });
    }
    if (url.endsWith("/tool")) {
      return response({
        result: {
          status: "ok",
          capsules: [{ path: "theme/read", content: "fait exact" }],
        },
        read_souvenirs: ["theme/read"],
        library_preflight_consumed: false,
      });
    }
    return response({ ingested: true });
  };

  await engine.ingest({
    sessionId: "session-canary",
    message: { role: "user", content: "Question" },
  });
  await engine.assemble({
    sessionId: "session-canary",
    messages: [],
    prompt: "Question",
  });
  const hookResult = await promptHook({}, { sessionId: "session-canary" });
  const memoryTool = tool("daemon_read_souvenirs", "session-canary");
  const recallTool = tool("daemon_recall_l0", "session-canary");
  assert.equal(memoryTool.parameters.properties.collection_offset.minimum, 0);
  assert.match(
    memoryTool.parameters.properties.collection_path.description,
    /collection marked/,
  );
  assert.match(
    memoryTool.parameters.properties.scene.properties.handling.properties.mode.description,
    /waits without composing/,
  );
  assert.match(
    memoryTool.parameters.properties.scene.properties.handling.properties.plan.description,
    /then waits without composing/,
  );
  assert.match(
    memoryTool.parameters.properties.scene.properties.established.description,
    /neither downgrades nor removes/,
  );
  assert.match(
    memoryTool.parameters.properties.scene.properties.uncertain.description,
    /an unknown about one property/,
  );
  assert.match(
    memoryTool.parameters.properties.scene.properties.uncertain.description,
    /The scope itself is uncertain/,
  );
  assert.deepEqual(
    memoryTool.parameters.properties.scene.properties.handling.properties.mode.enum,
    ["direct", "clarify", "condition"],
  );
  assert.doesNotMatch(
    memoryTool.parameters.properties.scene.properties.handling.properties.mode.description,
    /offer|nouveau sujet/,
  );
  assert.doesNotMatch(
    memoryTool.parameters.properties.scene.properties.handling.properties.plan.description,
    /offer|demande l'accord/,
  );
  assert.deepEqual(
    recallTool.parameters.properties.lookup_kind.enum,
    ["personal_fact", "conversation_chronology"],
  );
  assert.equal(recallTool.parameters.properties.temporal_context, undefined);
  const scene = {
    established: ["Une personne est explicitement concernée."],
    uncertain: ["Une autre personne pourrait modifier le choix."],
    need: "Établir les préférences utiles à la décision.",
    handling: {
      mode: "condition",
      plan: "Si elle est concernée aussi, le choix sera différent.",
    },
  };
  const toolResult = await memoryTool.execute("call-1", {
    paths: ["theme/read"],
    scene,
  });
  await engine.ingest({
    sessionId: "session-canary",
    message: { role: "assistant", content: "Réponse" },
  });

  const assemble = requests.find((entry) => entry.url.endsWith("/assemble"));
  const toolCall = requests.find((entry) => entry.url.endsWith("/tool"));
  const ingest = requests.find(
    (entry) => entry.url.endsWith("/ingest") && entry.body.role === "assistant",
  );
  assert.equal(Object.hasOwn(assemble.body, "sovereign_library"), false);
  assert.equal(assemble.body.session_id, "session-canary");
  assert.deepEqual(hookResult, {
    systemPrompt: withProgressPreamble("SYSTEME ATLAS"),
  });
  assert.equal(toolCall.body.name, "daemon_read_souvenirs");
  assert.equal(toolCall.body.trace_id, "trace-1");
  assert.equal(Object.hasOwn(toolCall.body, "sovereign_library"), false);
  assert.deepEqual(toolCall.body.arguments.scene, scene);
  assert.equal(toolResult.details.status, "ok");
  assert.equal(Object.hasOwn(ingest.body, "sovereign_library"), false);
  assert.equal(ingest.body.voice_path, "canonical");
  assert.equal(ingest.body.canary_trace_id, "trace-1");
  assert.deepEqual(ingest.body.read_souvenirs, ["theme/read"]);
});

test("an immediate repeated recall keeps the exact souvenir result", async () => {
  const { engine, tool } = setup();
  let toolRequests = 0;
  globalThis.fetch = async (url) => {
    if (url.endsWith("/assemble")) {
      return response({
        system_prompt_addition: "",
        sovereign_system_prompt: "SYSTEME ATLAS",
        souvenirs: ["theme/famille/grand_mere_annecy"],
        voice_path: "canonical",
        canary_trace_id: "trace-repeat",
        route: { mode: "canonical" },
      });
    }
    if (url.endsWith("/tool")) {
      toolRequests += 1;
      return response(toolRequests === 1
        ? {
            result: {
              status: "capsule_sources",
              capsules: [{
                path: "theme/famille/grand_mere_annecy",
                content: "La grand-mère de Camille vit seule à Annecy.",
              }],
            },
            read_souvenirs: ["theme/famille/grand_mere_annecy"],
            library_preflight_consumed: true,
          }
        : {
            result: { status: "not_found" },
            read_souvenirs: ["theme/famille/grand_mere_annecy"],
            library_preflight_consumed: true,
          });
    }
    return response({ ingested: true });
  };

  await engine.assemble({
    sessionId: "session-repeat-recall",
    messages: [],
    prompt: "Compare la météo avec chez ma grand-mère.",
  });
  const recall = tool("daemon_recall_l0", "session-repeat-recall");
  const args = {
    need: "La ville où vit la grand-mère de Camille.",
    lookup_kind: "personal_fact",
  };

  const first = await recall.execute("call-1", args);
  const repeated = await recall.execute("call-2", args);

  assert.equal(toolRequests, 1);
  assert.deepEqual(repeated.details, first.details);
  assert.equal(repeated.details.status, "capsule_sources");
  assert.match(repeated.details.capsules[0].content, /Annecy/);
});

test("professional calendar bridge is not registered by OpenClaw", () => {
  assert.equal(setup().tool("read_professional_calendar", "session"), null);
});

test("degraded host fallback leaves memory tools unavailable for the turn", async () => {
  const { engine, tool } = setup();
  globalThis.fetch = async (url) => {
    if (url.endsWith("/assemble")) {
      return response({
        system_prompt_addition: "CONTRAT DE SECOURS",
        sovereign_system_prompt: "",
        souvenirs: [],
        voice_path: "host_fallback",
        degraded: true,
        degradation_reason: "canonical_prepare_failed",
        route: { mode: "degraded" },
      });
    }
    throw new Error("tool endpoint must not be called");
  };

  await engine.assemble({
    sessionId: "session-fallback",
    messages: [],
    prompt: "Salut",
  });
  const result = await tool("daemon_read_souvenirs", "session-fallback")
    .execute("call-1", { paths: ["theme/x"] });

  assert.equal(result.details.status, "unavailable");
});

test("legacy voice switches cannot unregister canonical memory tools", () => {
  const { tool } = setup({
    legacySovereignVoice: false,
    legacySovereignLibrary: false,
  });
  assert.ok(tool("daemon_read_souvenirs", "session"));
  assert.ok(tool("daemon_recall_l0", "session"));
});
