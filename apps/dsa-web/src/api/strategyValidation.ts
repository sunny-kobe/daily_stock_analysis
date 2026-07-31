import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  StrategyTransitionRequest,
  StrategyTransitionResponse,
  StrategyValidationRun,
  StrategyVersion,
} from '../types/strategyValidation';

export const strategyValidationApi = {
  listStrategies: async (): Promise<StrategyVersion[]> => {
    const response = await apiClient.get<{ items?: unknown[] }>(
      '/api/v1/strategy-validation/strategies',
    );
    const payload = toCamelCase<{ items?: StrategyVersion[] }>(response.data);
    return payload.items ?? [];
  },

  getStrategy: async (strategyKey: string, version: string): Promise<StrategyVersion> => {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/strategy-validation/strategies/${encodeURIComponent(strategyKey)}/versions/${encodeURIComponent(version)}`,
    );
    return toCamelCase<StrategyVersion>(response.data);
  },

  getRun: async (runId: string): Promise<StrategyValidationRun> => {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/strategy-validation/runs/${encodeURIComponent(runId)}`,
    );
    return toCamelCase<StrategyValidationRun>(response.data);
  },

  transition: async (
    strategyKey: string,
    version: string,
    request: StrategyTransitionRequest,
  ): Promise<StrategyTransitionResponse> => {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/strategy-validation/strategies/${encodeURIComponent(strategyKey)}/versions/${encodeURIComponent(version)}/transition`,
      {
        to_status: request.toStatus,
        human_reason: request.humanReason,
      },
    );
    return toCamelCase<StrategyTransitionResponse>(response.data);
  },
};
