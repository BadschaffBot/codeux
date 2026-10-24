import type { NodeFlowRepository } from "../../repositories/node-flow-repository.js";
import type { NodeFlowRunRecord } from "../../contracts/node-flow-types.js";

export class NodeFlowQuotaExceededError extends Error {}
export class NodeFlowAdmissionDeferredError extends Error {}

export class NodeFlowQueueService {
  constructor(private readonly repository: NodeFlowRepository) {}

  claim(run: NodeFlowRunRecord, executorId: string): NodeFlowRunRecord {
    if (this.repository.countActiveRuns() >= run.policy.maxConcurrentRuns
      || this.repository.countActiveRuns(run.projectId) >= run.policy.maxConcurrentRunsPerProject) {
      const message = "Node flow concurrency quota is exhausted.";
      if (this.repository.terminalizeUnclaimedRun(run.id, message)) {
        throw new NodeFlowAdmissionDeferredError(message);
      }
      throw new NodeFlowQuotaExceededError(message);
    }
    const claimed = this.repository.claimQueuedRun(run.id, executorId, run.policy.leaseDurationMs);
    if (!claimed) throw new Error("Node flow run could not be claimed.");
    return claimed;
  }
}
