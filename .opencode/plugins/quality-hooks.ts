/**
 * OpenCode project plugin: auto-run local Python quality hooks.
 *
 * Triggers after OpenCode writes/edits JSON articles under `knowledge/articles/`.
 * Runs:
 *   - python3 hooks/validate_json.py <files...>
 *   - python3 hooks/check_quality.py <files...>
 *
 * Notes:
 * - Uses Bun's shell `$` injected by OpenCode plugin runtime.
 * - Debounces bursts of edits to avoid running hooks repeatedly.
 */
export default function qualityHooksPlugin(ctx: {
  directory: string
  worktree: string
  project: unknown
  client: { app: { log: (e: { service: string; level: string; message: string; extra?: unknown }) => Promise<void> } }
  $: (strings: TemplateStringsArray, ...values: unknown[]) => Promise<{ exitCode: number; stdout: string; stderr: string }>
}) {
  const { client, $, worktree } = ctx

  const service = "quality-hooks"

  // Debounce state
  let timer: ReturnType<typeof setTimeout> | undefined
  const pending = new Set<string>()

  function isTargetArticleJson(path: string): boolean {
    // Normalize to forward slashes for matching (Bun on Windows too, just in case).
    const p = path.replaceAll("\\", "/")
    return p.startsWith("knowledge/articles/") && p.endsWith(".json")
  }

  async function runHooks(filepaths: string[]) {
    if (filepaths.length === 0) return

    const files = [...new Set(filepaths)].sort()

    await client.app.log({
      service,
      level: "info",
      message: `Running hooks for ${files.length} file(s)`,
      extra: { files },
    })

    // Ensure we run from repo/worktree root so relative paths resolve.
    // Bun's $ supports `cwd` by prefixing with `cd`, but template literal is simplest/portable here.
    const validate = await $`cd ${worktree} && python3 hooks/validate_json.py ${files}`
    if (validate.exitCode !== 0) {
      await client.app.log({
        service,
        level: "warn",
        message: "validate_json failed",
        extra: { exitCode: validate.exitCode, stdout: validate.stdout, stderr: validate.stderr },
      })
      return
    }

    const quality = await $`cd ${worktree} && python3 hooks/check_quality.py ${files}`
    if (quality.exitCode !== 0) {
      await client.app.log({
        service,
        level: "warn",
        message: "check_quality failed",
        extra: { exitCode: quality.exitCode, stdout: quality.stdout, stderr: quality.stderr },
      })
      return
    }

    await client.app.log({
      service,
      level: "info",
      message: "Hooks passed",
      extra: { files },
    })
  }

  function scheduleRun() {
    if (timer) clearTimeout(timer)
    timer = setTimeout(async () => {
      const files = Array.from(pending)
      pending.clear()
      try {
        await runHooks(files)
      } catch (err) {
        await client.app.log({
          service,
          level: "error",
          message: "Hook runner crashed",
          extra: { error: String(err) },
        })
      }
    }, 1200)
  }

  return {
    "tool.execute.after": async (input: any) => {
      // We only care about local writes/edits that produce/modify knowledge articles.
      // Depending on OpenCode tool naming, these are typically "write" and "edit".
      const tool = input?.tool
      if (tool !== "write" && tool !== "edit") return

      const args = input?.args ?? {}

      // Handle both `filePath` and `path` styles.
      const rawPath = typeof args.filePath === "string" ? args.filePath : (typeof args.path === "string" ? args.path : "")
      if (!rawPath) return

      // OpenCode may provide absolute paths; convert to repo-relative if it is within worktree.
      let relPath = rawPath
      if (relPath.startsWith(worktree)) {
        relPath = relPath.slice(worktree.length).replace(/^\/+/, "")
      }

      if (!isTargetArticleJson(relPath)) return

      pending.add(relPath)
      scheduleRun()
    },
  }
}

