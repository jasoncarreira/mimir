// The dashboard owns React; a plugin must never bring its own hook dispatcher.
const React = window.mimirPluginHost!.react;
export default React;
