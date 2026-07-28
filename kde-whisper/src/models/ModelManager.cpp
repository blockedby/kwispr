#include "models/ModelManager.h"

#include <QDir>
#include <QProcess>

ModelManager::ModelManager(QString repoRoot,
                           QString catalogPath,
                           QString modelDir,
                           ProcessRunner *runner,
                           QObject *parent,
                           QString helperProgram)
    : QObject(parent)
    , m_repoRoot(std::move(repoRoot))
    , m_catalogPath(std::move(catalogPath))
    , m_modelDir(std::move(modelDir))
    , m_helperProgram(std::move(helperProgram))
    , m_runner(runner ? runner : &m_defaultRunner)
    , m_process(new QProcess(this))
{
    connect(m_process, &QProcess::readyReadStandardOutput, this, [this]() {
        m_stdout += QString::fromLocal8Bit(m_process->readAllStandardOutput());
    });
    connect(m_process, &QProcess::readyReadStandardError, this, [this]() {
        m_stderr += QString::fromLocal8Bit(m_process->readAllStandardError());
    });
    connect(m_process, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
            [this](int exitCode, QProcess::ExitStatus exitStatus) {
                if (!m_busy) {
                    return;
                }
                m_stdout += QString::fromLocal8Bit(m_process->readAllStandardOutput());
                m_stderr += QString::fromLocal8Bit(m_process->readAllStandardError());
                finishOperation(exitStatus == QProcess::NormalExit && exitCode == 0);
            });
    connect(m_process, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
        if (!m_busy) {
            return;
        }
        if (error == QProcess::FailedToStart) {
            finishOperation(false, m_process->errorString());
        }
    });
}

QMap<QString, bool> ModelManager::parseListOutput(const QString &output)
{
    QMap<QString, bool> statuses;
    const QStringList lines = output.split('\n', Qt::SkipEmptyParts);
    for (const QString &line : lines) {
        const QStringList columns = line.split('\t');
        if (columns.size() < 2) {
            continue;
        }
        statuses.insert(columns.at(0), columns.at(1) == QStringLiteral("installed"));
    }
    return statuses;
}

QMap<QString, bool> ModelManager::listInstalledStatus()
{
    const ProcessResult result = runHelper({QStringLiteral("list")});
    if (result.exitCode != 0) {
        return {};
    }
    return parseListOutput(result.stdoutText);
}

ProcessResult ModelManager::verify(const QString &modelId)
{
    return runHelper({QStringLiteral("verify"), modelId});
}

bool ModelManager::startDownload(const QString &modelId)
{
    return startOperation(QStringLiteral("download"), modelId);
}

bool ModelManager::startDelete(const QString &modelId)
{
    return startOperation(QStringLiteral("delete"), modelId);
}

bool ModelManager::isBusy() const
{
    return m_busy;
}

QStringList ModelManager::helperArguments(const QStringList &commandArguments) const
{
    QStringList arguments;
    arguments << QDir(m_repoRoot).filePath(QStringLiteral("kwispr-models.py"))
              << QStringLiteral("--catalog") << m_catalogPath
              << QStringLiteral("--model-dir") << m_modelDir;
    arguments << commandArguments;
    return arguments;
}

bool ModelManager::startOperation(const QString &operation, const QString &modelId)
{
    if (m_busy || modelId.isEmpty()) {
        return false;
    }

    m_busy = true;
    m_operation = operation;
    m_modelId = modelId;
    m_stdout.clear();
    m_stderr.clear();
    m_process->setProgram(m_helperProgram);
    m_process->setArguments(helperArguments({operation, modelId}));
    emit operationStarted(operation, modelId);
    m_process->start();
    return true;
}

void ModelManager::finishOperation(bool success, const QString &processError)
{
    if (!m_busy) {
        return;
    }
    if (!processError.isEmpty()) {
        if (!m_stderr.isEmpty() && !m_stderr.endsWith(QLatin1Char('\n'))) {
            m_stderr += QLatin1Char('\n');
        }
        m_stderr += processError;
    }

    const QString operation = m_operation;
    const QString modelId = m_modelId;
    const QString stdoutText = m_stdout;
    const QString stderrText = m_stderr;
    m_busy = false;
    m_operation.clear();
    m_modelId.clear();
    emit operationFinished(operation, modelId, success, stdoutText, stderrText);
}

ProcessResult ModelManager::runHelper(const QStringList &commandArguments)
{
    return m_runner->run(m_helperProgram, helperArguments(commandArguments));
}
