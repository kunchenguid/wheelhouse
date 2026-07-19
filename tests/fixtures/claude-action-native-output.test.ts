#!/usr/bin/env bun

import { expect, mock, spyOn, test } from "bun:test";
import { mkdtemp, writeFile } from "fs/promises";
import { tmpdir } from "os";
import { join } from "path";

const schemaValidResult = JSON.stringify({ ok: true });
const terminals = [
  {
    type: "result",
    subtype: "success",
    is_error: false,
    result: schemaValidResult,
    duration_ms: 10,
    num_turns: 1,
  },
  {
    type: "result",
    subtype: "success",
    is_error: false,
    result: schemaValidResult,
    structured_output: { ok: true },
    duration_ms: 10,
    num_turns: 1,
  },
];
let invocation = 0;

mock.module("@anthropic-ai/claude-agent-sdk", () => ({
  query: async function* () {
    yield {
      type: "system",
      subtype: "init",
      session_id: `session-${invocation}`,
      model: "claude-sonnet-4-6",
    };
    yield terminals[invocation++];
  },
}));

test("the pinned action enforces and captures native structured output", async () => {
  spyOn(console, "error").mockImplementation(() => {});
  spyOn(console, "log").mockImplementation(() => {});
  const core = await import("@actions/core");
  const setFailed = spyOn(core, "setFailed").mockImplementation(() => {});
  const root = await mkdtemp(join(tmpdir(), "wheelhouse-native-output-"));
  process.env.RUNNER_TEMP = root;
  const prompt = join(root, "prompt.txt");
  await writeFile(prompt, "Return the fixture object.");
  const { runClaudeWithSdk } = await import("../src/run-claude-sdk");
  const options = {
    sdkOptions: { extraArgs: { "json-schema": '{"type":"object"}' } },
    showFullOutput: false,
    hasJsonSchema: true,
  };

  await expect(runClaudeWithSdk(prompt, options)).rejects.toThrow(
    "--json-schema was provided but Claude did not return structured_output. Result subtype: success",
  );
  expect(setFailed).toHaveBeenCalledWith(
    "--json-schema was provided but Claude did not return structured_output. Result subtype: success",
  );

  const captured = await runClaudeWithSdk(prompt, options);
  expect(captured.conclusion).toBe("success");
  expect(captured.structuredOutput).toBe('{"ok":true}');
});
