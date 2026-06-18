import { opsDashboardFixture } from "../fixtures/api";
import {
  buildOpsSummaryMetrics,
  quotaRows,
  schedulerEventRows,
  tokenUsageRows
} from "./opsViewModel";

const summary = buildOpsSummaryMetrics(opsDashboardFixture.summary);
const quota = quotaRows(opsDashboardFixture.usage_history);
const tokenUsage = tokenUsageRows(opsDashboardFixture.token_usage_history);
const scheduler = schedulerEventRows(opsDashboardFixture);

const representativeOpsParsingCoverage: {
  summaryLabel: string;
  quotaLatest: number | null;
  tokenTurns: number;
  schedulerSignals: number;
} = {
  summaryLabel: summary.find((row) => row.key === "failures")?.label ?? "",
  quotaLatest: quota[0]?.latestUtilization ?? null,
  tokenTurns: tokenUsage[0]?.turns ?? 0,
  schedulerSignals: scheduler.length
};

void representativeOpsParsingCoverage;
