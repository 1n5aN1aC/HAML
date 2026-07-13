// Theme auto-discovery: every ./themes/*.css file becomes a selectable theme.
// The picker shows the filename exactly as written ("Light-sage.css" →
// "Light-sage"); the id is the filename normalized to lowercase with
// spaces/underscores → dashes ("light-sage"), and the file's
// [data-theme="..."] selector must equal that normalized id — it's what gets
// written to <html data-theme>. Importing this module also loads all theme
// CSS into the bundle, so dropping a new file into ./themes/ is the whole job
// of adding a theme.
const files = import.meta.glob('./themes/*.css', { eager: true })

export const THEMES = Object.keys(files)
  .map((path) => path.match(/([^/]+)\.css$/)[1])
  .sort((a, b) => a.localeCompare(b))
  .map((name) => ({
    id: name.toLowerCase().replace(/[\s_]+/g, '-').replace(/-+/g, '-'),
    label: name,
  }))

export const DEFAULT_THEME = 'light'

// Saved ids that no longer have a file (retired themes) fall back to the
// default; light.css's bare :root selector covers the pre-React paint.
export function validTheme(id) {
  return THEMES.some((t) => t.id === id) ? id : DEFAULT_THEME
}
