import type {
  ChatSendRequest,
  ChatSendResponse,
  ChatStreamEvent,
  EventsResponse,
  MemoryChannelsResponse,
  MemoryFileResponse,
  MemorySearchResponse,
  MemoryTreeResponse,
  OpsPayload,
  SagaActivationHistResponse,
  SagaAtomDetail,
  SagaClustersResponse,
  SagaRecentResponse,
  SagaSearchResponse,
  SagaSqlResponse,
  SagaStatsResponse,
  TurnsResponse
} from "../api/contracts";
import {
  chatMessageEventFixture,
  chatReactEventFixture,
  chatSendFixture,
  eventsFixture,
  memoryChannelsFixture,
  memoryFileFixture,
  memorySearchFixture,
  memoryTreeFixture,
  opsFixture,
  sagaActivationFixture,
  sagaAtomFixture,
  sagaClustersFixture,
  sagaRecentFixture,
  sagaSearchFixture,
  sagaSqlFixture,
  sagaStatsFixture,
  turnsFixture
} from "./parityApi";

export interface MockParityClient {
  listTurns(): Promise<TurnsResponse>;
  listEvents(): Promise<EventsResponse>;
  getOpsSummary(): Promise<OpsPayload>;
  getSagaStats(): Promise<SagaStatsResponse>;
  listSagaAtoms(): Promise<SagaRecentResponse>;
  getSagaAtom(): Promise<SagaAtomDetail>;
  searchSagaAtoms(): Promise<SagaSearchResponse>;
  getSagaActivationHistogram(): Promise<SagaActivationHistResponse>;
  getSagaClusters(): Promise<SagaClustersResponse>;
  runSagaSql(): Promise<SagaSqlResponse>;
  getMemoryTree(): Promise<MemoryTreeResponse>;
  getMemoryFile(): Promise<MemoryFileResponse>;
  searchMemory(): Promise<MemorySearchResponse>;
  listMemoryChannels(): Promise<MemoryChannelsResponse>;
  sendChatMessage(body: ChatSendRequest): Promise<ChatSendResponse>;
  chatStreamEvents(): AsyncIterable<ChatStreamEvent>;
}

async function* fixtureChatStream(): AsyncIterable<ChatStreamEvent> {
  yield chatMessageEventFixture;
  yield chatReactEventFixture;
}

export function createMockParityClient(): MockParityClient {
  return {
    listTurns: async () => turnsFixture,
    listEvents: async () => eventsFixture,
    getOpsSummary: async () => opsFixture,
    getSagaStats: async () => sagaStatsFixture,
    listSagaAtoms: async () => sagaRecentFixture,
    getSagaAtom: async () => sagaAtomFixture,
    searchSagaAtoms: async () => sagaSearchFixture,
    getSagaActivationHistogram: async () => sagaActivationFixture,
    getSagaClusters: async () => sagaClustersFixture,
    runSagaSql: async () => sagaSqlFixture,
    getMemoryTree: async () => memoryTreeFixture,
    getMemoryFile: async () => memoryFileFixture,
    searchMemory: async () => memorySearchFixture,
    listMemoryChannels: async () => memoryChannelsFixture,
    sendChatMessage: async (_body: ChatSendRequest) => chatSendFixture,
    chatStreamEvents: fixtureChatStream
  };
}
