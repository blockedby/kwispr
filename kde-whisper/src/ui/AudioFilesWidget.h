#pragma once

#include <QWidget>
#include <QStringList>
#include <QQueue>
#include <QJsonObject>
#include <functional>

class QLabel;
class QLineEdit;
class QListWidget;
class QPlainTextEdit;
class QProcess;
class QProgressBar;
class QPushButton;

class AudioFilesWidget : public QWidget
{
    Q_OBJECT
public:
    using FolderOpenCompletion = std::function<void(bool, const QString &)>;
    using FolderOpener = std::function<void(const QString &, FolderOpenCompletion)>;
    explicit AudioFilesWidget(QString runtimeRoot, QString configPath, FolderOpener folderOpener, QWidget *parent = nullptr);
    ~AudioFilesWidget() override;
    bool hasPendingWork() const;
    void enqueueFiles(const QStringList &paths);

private:
    struct Job { QString source; QString outputRoot; int row; };
    void startNext();
    void readOutput();
    void consumeLine(const QByteArray &line);
    void finishJob(int exitCode, bool normalExit);
    void updateActions();
    void showSelectedResult();
    QString m_runtimeRoot;
    QString m_configPath;
    FolderOpener m_folderOpener;
    QQueue<Job> m_queue;
    Job m_current;
    bool m_running = false;
    bool m_hadResult = false;
    bool m_folderOpenBusy = false;
    QByteArray m_stdoutBuffer;
    QJsonObject m_result;
    QString m_error;
    QProcess *m_process;
    QLineEdit *m_outputEdit;
    QListWidget *m_jobs;
    QPlainTextEdit *m_transcript;
    QLabel *m_status;
    QLabel *m_errorLabel;
    QProgressBar *m_progress;
    QPushButton *m_addButton;
    QPushButton *m_browseButton;
    QPushButton *m_copyButton;
    QPushButton *m_openButton;
};
