from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
BACKUP_SCRIPT = ROOT / "ghostfolio" / "backup-and-restore-test.ps1"
INSTALL_SCRIPT = ROOT / "ghostfolio" / "install-backup-task.ps1"


class GhostfolioBackupScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backup = BACKUP_SCRIPT.read_text(encoding="utf-8")
        cls.installer = INSTALL_SCRIPT.read_text(encoding="utf-8")

    def test_backup_waits_for_docker_and_healthy_postgres(self):
        self.assertIn("function Wait-GhostfolioPostgres", self.backup)
        self.assertIn("docker info", self.backup)
        self.assertIn(".State.Health.Status", self.backup)
        self.assertIn("DockerWaitAttempts", self.backup)
        self.assertIn("DockerWaitSeconds", self.backup)
        self.assertIn("$composeExit = $LASTEXITCODE", self.backup)
        self.assertIn("$inspectExit = $LASTEXITCODE", self.backup)

    def test_retention_is_bounded_safe_and_runs_after_restore(self):
        self.assertIn("[int]$RetentionCount = 14", self.backup)
        self.assertIn("Select-Object -Skip $RetentionCount", self.backup)
        self.assertIn("StartsWith($resolvedRoot", self.backup)
        retention_call = self.backup.rfind("\nRemove-ExpiredBackups\n")
        restore_check = self.backup.rfind("pg_restore")
        self.assertGreater(retention_call, restore_check)

    def test_restore_uses_the_deployed_postgres_image(self):
        self.assertIn("$postgresImageId", self.backup)
        self.assertIn("docker inspect --format '{{.Image}}'", self.backup)
        self.assertNotIn("postgres:15-alpine | Out-Null", self.backup)

    def test_task_has_catch_up_retry_and_overlap_controls(self):
        self.assertIn("StartWhenAvailable = $true", self.installer)
        self.assertIn("RestartCount = 6", self.installer)
        self.assertIn("New-TimeSpan -Minutes 10", self.installer)
        self.assertIn("MultipleInstances = 'IgnoreNew'", self.installer)
        self.assertIn("AllowStartIfOnBatteries = $true", self.installer)
        self.assertIn("DontStopIfGoingOnBatteries = $true", self.installer)

    def test_wrapper_reports_a_real_exit_code_without_secret_values(self):
        self.assertIn("backup-task-status.json", self.installer)
        self.assertIn("exit $exitCode", self.installer)
        self.assertIn("$record.status = 'Failed'", self.installer)
        self.assertIn("$record.status = 'Succeeded'", self.installer)
        self.assertIn("$record.errorCode = $_.Exception.Message", self.installer)
        self.assertNotIn("DCA_GHOSTFOLIO_SECRETS_FILE", self.installer)
        self.assertIn("/inheritance:r", self.installer)


if __name__ == "__main__":
    unittest.main()
