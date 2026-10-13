# Despachador de Revendedor WA

Este Worker no procesa imágenes. Su única tarea es activar el workflow Python del repositorio cada cinco minutos. El análisis visual/OCR, generación de carteles, Drive y Telegram se ejecutan en GitHub Actions.

## Configuración de Cloudflare

- Código: `pumprecios-publicador.js`
- Cron Trigger: `*/5 * * * *`
- Secret obligatorio: `GITHUB_ACTIONS_TOKEN`

El token de GitHub debe ser Fine-grained y limitarse al repositorio `cholovic1-arch/revendedor-wa-cloud`, con permiso de repositorio **Actions: Read and write**. No hace falta permiso de Contents para llamar al endpoint de dispatch; se puede dejar sin acceso de escritura a Contents. Nunca poner el token en el código ni enviarlo por chat.

## Desplegar desde la carpeta cloudflare

Con Node.js instalado y Wrangler autenticado en la cuenta Cloudflare:

```powershell
cd cloudflare
npx wrangler secret put GITHUB_ACTIONS_TOKEN
npx wrangler deploy
```

Wrangler toma el cron desde `wrangler.toml`. Para comprobar el Worker, abrir su URL debe mostrar `Revendedor WA dispatcher activo`. En Cloudflare, revisar **Workers & Pages → pumprecios-publicador → Triggers** y confirmar que aparece el cron cada cinco minutos.

## Revisión del resultado

Abrir GitHub Actions en el repositorio. Cada disparo correcto del Worker crea una ejecución con evento `workflow_dispatch`. Si no aparece, revisar en Cloudflare los logs del Worker y el secreto `GITHUB_ACTIONS_TOKEN`.

## IA visual

El workflow pasa los secretos `GEMINI_API_KEY` y `OPENROUTER_API_KEY` al código. Configurar al menos uno en GitHub Actions Secrets para análisis visual. OpenRouter usa solo un modelo marcado gratuito como respaldo. Si no se configura ninguna clave, el sistema avisa en el log y usa OCR; no finge que hubo análisis por IA.
