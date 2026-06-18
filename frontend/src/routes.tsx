import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet
} from "@tanstack/react-router";
import { shellRoutes } from "./routeConfig";
import { AppShell } from "./shell";
import type { AppSearch, ShellRoute } from "./types";

function readSearch(search: Record<string, unknown>): AppSearch {
  return {
    turn: readString(search.turn),
    tab: readString(search.tab),
    filter: readString(search.filter),
    detail: readString(search.detail)
  };
}

function readString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function RouteFrame({ route }: { route: ShellRoute }) {
  return <AppShell route={route} />;
}

const rootRoute = createRootRoute({
  component: () => <Outlet />
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: () => <AppShell route={shellRoutes[0]} />
});

const childRoutes = shellRoutes.map((route) =>
  createRoute({
    getParentRoute: () => rootRoute,
    path: route.path,
    validateSearch: readSearch,
    component: () => <RouteFrame route={route} />
  })
);

const routeTree = rootRoute.addChildren([indexRoute, ...childRoutes]);

export const router = createRouter({
  routeTree,
  basepath: "/app",
  defaultPreload: "intent"
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
