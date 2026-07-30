#pragma once

#include <QUrl>
#include <QString>

struct LocalVadStatus
{
    bool enabled = false;
    QString provider;
    QString modelPath;
    double threshold = 0.0;
    unsigned int frameMs = 0;
    unsigned int minSpeechMs = 0;
    unsigned int paddingMs = 0;
};

enum class LocalSttState
{
    Healthy,
    Stopped,
    Unhealthy,
};

struct LocalSttStatus
{
    LocalSttState state = LocalSttState::Unhealthy;
    QString statusText;
    LocalVadStatus vad;
    QString errorText;
};

class LocalSttClient
{
public:
    explicit LocalSttClient(QUrl baseUrl = QUrl(QStringLiteral("http://127.0.0.1:19650")));

    LocalSttStatus checkHealth(int timeoutMs = 3000) const;

private:
    QUrl baseUrl_;
};
