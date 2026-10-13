const host = window.mimirPluginHost;
if (host?.apiMajor !== 1) throw new Error("Example requires plugin host v1");
function ExamplePage() {
  return host.react.createElement("section", { className: "example-page" }, "Example plugin dashboard");
}
host.registerRoute({ plugin: "example", id: "example-page", path: "/example", component: ExamplePage });
host.registerNavEntry({ plugin: "example", id: "example-page", label: "Example" });
