import type { ShellRoute } from "./types";

export const shellRoutes: ShellRoute[] = [
  { id: "chat", path: "/chat", label: "Chat", summary: "Operator chat frame." },
  {
    id: "turns",
    path: "/turns",
    label: "Turn Viewer",
    summary: "Turn selection and log viewing frame.",
    legacyHref: "/turns"
  },
  {
    id: "ops",
    path: "/ops",
    label: "Ops",
    summary: "Operations dashboard frame.",
    legacyHref: "/ops"
  },
  {
    id: "saga",
    path: "/saga",
    label: "SAGA",
    summary: "SAGA memory dashboard frame.",
    legacyHref: "/saga"
  },
  {
    id: "memory",
    path: "/memory",
    label: "State/Memory",
    summary: "State and memory browser frame.",
    legacyHref: "/state"
  },
  {
    id: "hermes",
    path: "/hermes",
    label: "Hermes Gaps",
    summary: "Reserved frame for later Hermes-gap pages."
  }
];
