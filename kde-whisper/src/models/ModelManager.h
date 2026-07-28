#pragma once

#include "runtime/ProcessRunner.h"

#include <QByteArray>
#include <QMap>
#include <QObject>
#include <QString>
#include <QStringList>

class QProcess;

class ModelManager : public QObject
{
    Q_OBJECT
public:
    explicit ModelManager(QString repoRoot,
                          QString catalogPath,
                          QString modelDir,
                          ProcessRunner *runner = nullptr,
                          QObject *parent = nullptr,
                          QString helperProgram = QStringLiteral("python3"));

    static QMap<QString, bool> parseListOutput(const QString &output);

    QMap<QString, bool> listInstalledStatus();
    ProcessResult verify(const QString &modelId);

    virtual bool startDownload(const QString &modelId);
    virtual bool startDelete(const QString &modelId);
    bool isBusy() const;

    QStringList helperArguments(const QStringList &commandArguments) const;

signals:
    void operationStarted(const QString &operation, const QString &modelId);
    void operationFinished(const QString &operation,
                           const QString &modelId,
                           bool success,
                           const QString &stdoutText,
                           const QString &stderrText);
    void downloadStatus(const QString &modelId,
                        const QString &status,
                        qint64 bytesDone,
                        qint64 bytesTotal);
    void downloadProgress(const QString &modelId,
                          qint64 bytesDone,
                          qint64 bytesTotal);

private:
    bool startOperation(const QString &operation, const QString &modelId);
    void consumeStandardOutput(const QByteArray &chunk);
    void handleDownloadLine(const QByteArray &line);
    void finishOperation(bool success, const QString &processError = QString());
    ProcessResult runHelper(const QStringList &commandArguments);

    QString m_repoRoot;
    QString m_catalogPath;
    QString m_modelDir;
    QString m_helperProgram;
    ProcessRunner *m_runner;
    ProcessRunner m_defaultRunner;
    QProcess *m_process = nullptr;
    QString m_operation;
    QString m_modelId;
    QByteArray m_stdout;
    QByteArray m_downloadLineBuffer;
    QString m_stderr;
    bool m_busy = false;
};
