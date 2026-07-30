#include <QtTest/QtTest>

#include "runtime/KwisprController.h"
#include "runtime/ProcessRunner.h"
#include "runtime/RetryState.h"

class RecordingProcessRunner final : public ProcessRunner
{
public:
    ProcessResult nextResult;
    QString lastProgram;
    QStringList lastArguments;
    QProcessEnvironment lastEnvironment;
    int calls = 0;

    ProcessResult run(const QString &program,
                      const QStringList &arguments,
                      const QProcessEnvironment &environment = QProcessEnvironment::systemEnvironment()) override
    {
        ++calls;
        lastProgram = program;
        lastArguments = arguments;
        lastEnvironment = environment;
        return nextResult;
    }
};

class ProcessRunnerTest : public QObject
{
    Q_OBJECT

private slots:
    void toggleRecordingUsesExistingScript();
    void retryPassesPathAsArgument();
    void retryStateSupportsRawAndLegacyFormats();
    void failuresSurfaceExitCodeAndStderr();
};

void ProcessRunnerTest::toggleRecordingUsesExistingScript()
{
    RecordingProcessRunner runner;
    runner.nextResult = ProcessResult{0, QStringLiteral("ok"), QString()};

    KwisprController controller(QStringLiteral("/repo"), &runner);
    const ProcessResult result = controller.toggleRecording();

    QCOMPARE(result.exitCode, 0);
    QCOMPARE(runner.calls, 1);
    QCOMPARE(runner.lastProgram, QStringLiteral("/repo/kwispr.sh"));
    QCOMPARE(runner.lastArguments, QStringList{QStringLiteral("toggle")});
}

void ProcessRunnerTest::retryPassesPathAsArgument()
{
    RecordingProcessRunner runner;
    runner.nextResult = ProcessResult{0, QString(), QString()};

    KwisprController controller(QStringLiteral("/repo"), &runner);
    const QString retryPath = QStringLiteral("/tmp/kwispr failed/audio one.wav");
    controller.retry(retryPath);

    QCOMPARE(runner.calls, 1);
    QCOMPARE(runner.lastProgram, QStringLiteral("/repo/kwispr.sh"));
    QCOMPARE(runner.lastArguments, (QStringList{QStringLiteral("retry"), retryPath}));
}

void ProcessRunnerTest::retryStateSupportsRawAndLegacyFormats()
{
    const QString wavPath = QStringLiteral("/tmp/kwispr failed/audio one.wav");
    QCOMPARE(retryWavPathFromState(wavPath + QLatin1Char('\n')), wavPath);
    QCOMPARE(retryWavPathFromState(
                 QStringLiteral("/opt/kwispr/kwispr.sh retry \"/tmp/kwispr failed/audio one.wav\"\n")),
             wavPath);
}

void ProcessRunnerTest::failuresSurfaceExitCodeAndStderr()
{
    RecordingProcessRunner runner;
    runner.nextResult = ProcessResult{23, QString(), QStringLiteral("boom")};

    KwisprController controller(QStringLiteral("/repo"), &runner);
    const ProcessResult result = controller.toggleRecording();

    QCOMPARE(result.exitCode, 23);
    QCOMPARE(result.stderrText, QStringLiteral("boom"));
}

QTEST_MAIN(ProcessRunnerTest)
#include "process_runner_test.moc"
