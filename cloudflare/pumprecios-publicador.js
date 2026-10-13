/**
 * Despachador de Revendedor WA.
 * Configurar un Cron Trigger en Cloudflare para ejecutarse cada cinco minutos
 * Crear el secret GITHUB_ACTIONS_TOKEN con un Fine-grained PAT:
 * Repository: cholovic1-arch/revendedor-wa-cloud
 * Repository permissions: Actions = Read and write; Contents = Read-only.
 *
 * No procesa imágenes aquí: activa el workflow Python de GitHub Actions.
 */
const OWNER = "cholovic1-arch";
const REPO = "revendedor-wa-cloud";
const WORKFLOW = "revendedor-wa.yml";
const BRANCH = "main";

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(dispatchWorkflow(env));
  },

  async fetch(request, env) {
    if (request.method !== "GET") {
      return new Response("Method not allowed", { status: 405 });
    }
    return new Response(
      "Revendedor WA dispatcher activo. El procesamiento de fotos corre en GitHub Actions.",
      { status: 200, headers: { "content-type": "text/plain; charset=utf-8" } }
    );
  }
};

async function dispatchWorkflow(env) {
  const token = env.GITHUB_ACTIONS_TOKEN;
  if (!token) {
    console.error("Falta el secret GITHUB_ACTIONS_TOKEN en Cloudflare.");
    throw new Error("Falta GITHUB_ACTIONS_TOKEN");
  }

  const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "pumprecios-publicador"
    },
    body: JSON.stringify({ ref: BRANCH })
  });

  if (response.status !== 204) {
    const details = (await response.text()).slice(0, 1000);
    console.error("GitHub workflow_dispatch falló:", response.status, details);
    throw new Error(`GitHub respondió HTTP ${response.status}`);
  }

  console.log("Workflow Revendedor WA activado correctamente:", new Date().toISOString());
}
