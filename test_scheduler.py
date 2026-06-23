import unittest

from scheduler import _sanitize_analysis


SNAPSHOT = """=== NODES ===
  ip-10-0-0-1  Ready  v1.30.14-eks-ecaa3a6

=== DEPLOYMENTS ===
  app/api  desired=2 ready=2
  app/worker  desired=3 ready=2 ⚠
  app/web  desired=1 ready=1

=== PODS === all 12 pods healthy

=== HPAs ===
  app/worker  5/5 ⚠ AT MAX
"""


class SchedulerGuardTest(unittest.TestCase):
    def test_strips_invalid_versions_and_deployment_aggregates(self):
        report = """[WARNING]
- Node version v1.30.14-eks is old.
- 38 of 40 deployments at desired replicas.
- Node version v1.30.14-eks-ecaa3a6 appears on a Ready node.
- 2 of 3 deployments at desired replicas.
"""

        sanitized = _sanitize_analysis(SNAPSHOT, report)

        self.assertNotIn("v1.30.14-eks is old", sanitized)
        self.assertNotIn("38 of 40 deployments", sanitized)
        self.assertIn("v1.30.14-eks-ecaa3a6", sanitized)
        self.assertIn("2 of 3 deployments", sanitized)

    def test_strips_unsupported_factual_claims_but_keeps_recommendations(self):
        report = """[WARNING]
- HPA at max indicates sustained demand and queue latency.
- Health check reports no resource constraints detected.
- Verify queue depth with application metrics before attributing HPA max replicas to demand.
"""

        sanitized = _sanitize_analysis(SNAPSHOT, report)

        self.assertNotIn("sustained demand", sanitized)
        self.assertNotIn("no resource constraints detected", sanitized)
        self.assertIn("Verify queue depth", sanitized)


if __name__ == "__main__":
    unittest.main()
