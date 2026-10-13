/**
 * Revendedor WA: entrada segura para un programador externo.
 * Configurar en Cloudflare el secret CRON_DISPATCH_SECRET.
 * Un cron-job.org gratuito envía POST /run cada cinco minutos con:
 * Authorization: Bearer <CRON_DISPATCH_SECRET>
 */
const OWNER = "cholovic1-arch";
const REPO = "revendedor-wa-cloud";
const WORKFLOW = "revendedor-wa.yml";
const BRANCH = "main";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      return new Response("Revendedor WA: endpoint activo.", {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" }
      });
    }

    if (url.pathname !== "/run" || request.method !== "POST") {
      return new Response("Not found", { status: 404 });
    }

    const expected = (env.CRON_DISPATCH_SECRET || "").trim();
    const supplied = (request.headers.get("Authorization") || "").trim();
    if (!expected || supplied !== `Bearer ${expected}`) {
      return new Response("Unauthorized", { status: 401 });
    }

    try {
      await dispatchWorkflow(env);
      return new Response("Revendedor WA: ejecución solicitada a GitHub Actions.", {
        status: 202,
        headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" }
      });
    } catch (error) {
      console.error("No se pudo activar Revendedor WA:", error);
      return new Response("No se pudo activar el procesamiento. Revisar logs del Worker.", { status: 502 });
    }
  }
};

async function dispatchWorkflow(env) {
  const token = env.GITHUB_ACTIONS_TOKEN;
  if (!token) throw new Error("Falta GITHUB_ACTIONS_TOKEN");

  const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "pumprecios-publicador",
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ ref: BRANCH })
  });

  if (response.status !== 204) {
    const details = (await response.text()).slice(0, 500);
    throw new Error(`GitHub HTTP ${response.status}: ${details}`);
  }
  console.log("Workflow despachado correctamente:", new Date().toISOString());
}
