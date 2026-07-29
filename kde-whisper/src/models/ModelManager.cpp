#include "models/ModelManager.h"

#include <QDir>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QSet>

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
        consumeStandardOutput(m_process->readAllStandardOutput());
    });
    connect(m_process, &QProcess::readyReadStandardError, this, [this]() {
        m_stderr += QString::fromUtf8(m_process->readAllStandardError());
    });
    connect(m_process, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
            [this](int exitCode, QProcess::ExitStatus exitStatus) {
                if (!m_busy) {
                    return;
                }
                consumeStandardOutput(m_process->readAllStandardOutput());
                m_stderr += QString::fromUtf8(m_process->readAllStandardError());
                if (m_operation == QLatin1String("download") && !m_downloadLineBuffer.isEmpty()) {
                    handleDownloadLine(m_downloadLineBuffer);
                    m_downloadLineBuffer.clear();
                }
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
    m_downloadLineBuffer.clear();
    m_stderr.clear();
    m_process->setProgram(m_helperProgram);
    QStringList commandArguments{operation, modelId};
    if (operation == QLatin1String("download")) {
        commandArguments << QStringLiteral("--progress") << QStringLiteral("jsonl");
    }
    m_process->setArguments(helperArguments(commandArguments));
    emit operationStarted(operation, modelId);
    m_process->start();
    return true;
}

void ModelManager::consumeStandardOutput(const QByteArray &chunk)
{
    if (chunk.isEmpty()) {
        return;
    }
    m_stdout += chunk;
    if (m_operation != QLatin1String("download")) {
        return;
    }

    m_downloadLineBuffer += chunk;
    qsizetype newline = -1;
    while ((newline = m_downloadLineBuffer.indexOf('\n')) >= 0) {
        QByteArray line = m_downloadLineBuffer.left(newline);
        m_downloadLineBuffer.remove(0, newline + 1);
        if (line.endsWith('\r')) {
            line.chop(1);
        }
        handleDownloadLine(line);
    }
}

void ModelManager::handleDownloadLine(const QByteArray &line)
{
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(line, &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
        return;
    }
    const QJsonObject object = document.object();
    if (object.value(QStringLiteral("protocol")).toString() != QLatin1String("kwispr-model-download-v1")
        || object.value(QStringLiteral("model_slug")).toString() != m_modelId) {
        return;
    }
    const QJsonValue doneValue = object.value(QStringLiteral("bytes_done"));
    const QJsonValue totalValue = object.value(QStringLiteral("bytes_total"));
    if (!doneValue.isDouble() || !totalValue.isDouble()) {
        return;
    }
    const qint64 bytesDone = doneValue.toInteger(-1);
    const qint64 bytesTotal = totalValue.toInteger(-1);
    if (bytesDone < 0 || bytesTotal <= 0 || bytesDone > bytesTotal) {
        return;
    }

    const QString event = object.value(QStringLiteral("event")).toString();
    if (event == QLatin1String("progress")) {
        emit downloadProgress(m_modelId, bytesDone, bytesTotal);
        return;
    }
    if (event == QLatin1String("reset")) {
        const QJsonValue attempt = object.value(QStringLiteral("attempt"));
        if (bytesDone != 0 || !attempt.isDouble() || attempt.toInteger(0) <= 1) {
            return;
        }
        emit downloadStatus(m_modelId, QStringLiteral("reset"), bytesDone, bytesTotal);
        emit downloadProgress(m_modelId, 0, bytesTotal);
        return;
    }
    if (event == QLatin1String("attempt")) {
        const QJsonValue attempt = object.value(QStringLiteral("attempt"));
        if (bytesDone != 0 || !attempt.isDouble() || attempt.toInteger(0) <= 0) {
            return;
        }
        emit downloadStatus(m_modelId, QStringLiteral("attempt"), bytesDone, bytesTotal);
        return;
    }
    if (event != QLatin1String("status")) {
        return;
    }
    const QString phase = object.value(QStringLiteral("phase")).toString();
    static const QSet<QString> validPhases{
        QStringLiteral("downloading"),
        QStringLiteral("checksum-verification"),
        QStringLiteral("installing"),
        QStringLiteral("already-installed"),
        QStringLiteral("done"),
        QStringLiteral("failed"),
    };
    if (validPhases.contains(phase)) {
        emit downloadStatus(m_modelId, phase, bytesDone, bytesTotal);
    }
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
    const QString stdoutText = QString::fromUtf8(m_stdout);
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
