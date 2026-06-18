import React from "react";
import { AgentCharacterDemo } from "../agent-character";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  DataTable,
  Dialog,
  Drawer,
  EmptyState,
  ErrorState,
  LoadingState,
  LogBlock,
  NavList,
  Panel,
  Tabs,
  Timeline,
  ToastRegion
} from ".";

export function DashboardUiExamples() {
  const [dialogOpen, setDialogOpen] = React.useState(false);
  const [drawerOpen, setDrawerOpen] = React.useState(false);

  return (
    <Panel
      actions={
        <>
          <Button onClick={() => setDrawerOpen(true)}>Open drawer</Button>
          <Button onClick={() => setDialogOpen(true)} variant="primary">Open dialog</Button>
        </>
      }
      subtitle="Story-style examples for the dashboard design system primitives."
      title="Dashboard UI examples"
    >
      <Tabs
        label="UI primitive examples"
        items={[
          {
            id: "overview-example",
            label: "Overview",
            panel: (
              <div className="ui-card-grid">
                <Card title="Navigation">
                  <NavList
                    label="Example navigation"
                    items={[
                      { href: "/turns", label: "Turns", detail: "Turn viewer" },
                      { href: "/ops", label: "Ops", detail: "Operations" }
                    ]}
                  />
                </Card>
                <Card title="Status">
                  <p>
                    <Badge tone="info">queued</Badge>{" "}
                    <Badge tone="success">healthy</Badge>{" "}
                    <Badge tone="warning">slow</Badge>{" "}
                    <Badge tone="danger">failed</Badge>
                  </p>
                </Card>
              </div>
            )
          },
          {
            id: "data-example",
            label: "Data",
            panel: (
              <DataTable
                caption="Example worker states"
                columns={[
                  { key: "name", header: "Name" },
                  { key: "status", header: "Status" },
                  { key: "age", header: "Age" }
                ]}
                rows={[
                  { name: "worklink-550", status: <Badge tone="success">ready</Badge>, age: "2m" },
                  { name: "review-524", status: <Badge tone="warning">waiting</Badge>, age: "14m" }
                ]}
              />
            )
          },
          {
            id: "logs-example",
            label: "Logs",
            panel: (
              <div className="ui-card-grid">
                <CodeBlock code={"npm test\nnpm run build"} language="sh" title="Validation" />
                <LogBlock lines={["10:14 worker claimed issue", "10:15 tests passed"]} />
              </div>
            )
          },
          {
            id: "states-example",
            label: "States",
            panel: (
              <div className="ui-card-grid">
                <EmptyState title="No records">The selected filter has no matching records.</EmptyState>
                <ErrorState title="Fetch failed">The API returned a non-200 response.</ErrorState>
                <LoadingState label="Loading records" />
                <Card title="Agent character">
                  <AgentCharacterDemo />
                </Card>
              </div>
            )
          },
          {
            id: "timeline-example",
            label: "Timeline",
            panel: (
              <Timeline
                items={[
                  { title: "Claimed", meta: "10:14", detail: "Worker accepted the issue." },
                  { title: "Validated", meta: "10:21", detail: "Focused checks passed." }
                ]}
              />
            )
          }
        ]}
      />

      <Dialog open={dialogOpen} title="Confirm action" onClose={() => setDialogOpen(false)}>
        <p>Dialogs trap focus, close on Escape, and restore focus to the opener.</p>
        <Button onClick={() => setDialogOpen(false)} variant="primary">Done</Button>
      </Dialog>
      <Drawer open={drawerOpen} title="Details" onClose={() => setDrawerOpen(false)}>
        <p>Drawers use the same modal focus behavior while preserving side-panel layout.</p>
      </Drawer>
      <ToastRegion toasts={[{ id: "example-toast", tone: "success", message: "Example toast" }]} />
    </Panel>
  );
}
