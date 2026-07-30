#include "config/EnvFile.h"

#include <QFile>
#include <QFileDevice>
#include <QRegularExpression>
#include <QSaveFile>
#include <QTextStream>

bool EnvFile::load(const QString &path) {
    m_lines.clear();
    m_indexByKey.clear();
    m_error.clear();

    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        m_error = file.errorString();
        return false;
    }

    QTextStream in(&file);
    while (!in.atEnd()) {
        const QString raw = in.readLine();
        QString key;
        QString parsedValue;
        if (parseAssignment(raw, &key, &parsedValue)) {
            Line line;
            line.kind = LineKind::Assignment;
            line.raw = raw;
            line.key = key;
            line.value = parsedValue;
            m_indexByKey.insert(key, m_lines.size());
            m_lines.append(line);
        } else {
            Line line;
            line.kind = LineKind::Raw;
            line.raw = raw;
            m_lines.append(line);
        }
    }

    return true;
}

bool EnvFile::save(const QString &path) {
    m_error.clear();

    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Text)) {
        m_error = file.errorString();
        return false;
    }
    file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner);

    QTextStream out(&file);
    for (const Line &line : std::as_const(m_lines)) {
        if (line.kind == LineKind::Assignment) {
            out << formatAssignment(line.key, line.value) << '\n';
        } else {
            out << line.raw << '\n';
        }
    }

    out.flush();
    if (out.status() != QTextStream::Ok) {
        m_error = QStringLiteral("failed to write env file");
        file.cancelWriting();
        return false;
    }
    file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner);
    if (!file.commit()) {
        m_error = file.errorString();
        return false;
    }
    return true;
}

QString EnvFile::value(const QString &key, const QString &fallback) const {
    const auto it = m_indexByKey.constFind(key);
    if (it == m_indexByKey.constEnd()) {
        return fallback;
    }
    return m_lines.at(*it).value;
}

void EnvFile::setValue(const QString &key, const QString &value) {
    const auto it = m_indexByKey.constFind(key);
    if (it != m_indexByKey.constEnd()) {
        m_lines[*it].value = value;
        return;
    }

    Line line;
    line.kind = LineKind::Assignment;
    line.key = key;
    line.value = value;
    m_indexByKey.insert(key, m_lines.size());
    m_lines.append(line);
}

bool EnvFile::contains(const QString &key) const {
    return m_indexByKey.contains(key);
}

QString EnvFile::errorString() const {
    return m_error;
}

QMap<QString, QString> EnvFile::defaults() {
    return {
        {QStringLiteral("KWISPR_BACKEND"), QStringLiteral("openai-transcriptions")},
        {QStringLiteral("KWISPR_API_URL"), QStringLiteral("https://api.openai.com/v1/audio/transcriptions")},
        {QStringLiteral("KWISPR_LOCAL_STT_HOST"), QStringLiteral("127.0.0.1")},
        {QStringLiteral("KWISPR_LOCAL_STT_PORT"), QStringLiteral("19650")},
        {QStringLiteral("KWISPR_API_KEY"), QString()},
        {QStringLiteral("KWISPR_MODEL"), QStringLiteral("whisper-1")},
        {QStringLiteral("KWISPR_AUDIO_FORMAT"), QStringLiteral("wav")},
        {QStringLiteral("KWISPR_PULSE_SOURCE"), QStringLiteral("default")},
        {QStringLiteral("KWISPR_LANGUAGE"), QString()},
        {QStringLiteral("KWISPR_AUTOPASTE"), QStringLiteral("1")},
        {QStringLiteral("KWISPR_PASTE_HOTKEY"), QStringLiteral("shift-insert")},
        {QStringLiteral("KWISPR_AUTOPASTE_DELAY"), QStringLiteral("0.30")},
        {QStringLiteral("KWISPR_SOUNDS"), QStringLiteral("1")},
        {QStringLiteral("KWISPR_TRANSCRIPTION_PROMPT"), QStringLiteral("Transcribe the audio exactly. Preserve the speaker's language, wording, and tone. Add punctuation only where it is clearly implied by speech.")},
    };
}

bool EnvFile::parseAssignment(const QString &line, QString *key, QString *value) {
    static const QRegularExpression re(QStringLiteral("^\\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$"));
    const auto match = re.match(line);
    if (!match.hasMatch()) {
        return false;
    }
    *key = match.captured(1);
    *value = parseValue(match.captured(2));
    return true;
}

QString EnvFile::parseValue(QString text) {
    text = text.trimmed();
    if (text.isEmpty()) {
        return QString();
    }

    if (text.startsWith('\'') && text.endsWith('\'') && text.size() >= 2) {
        QString inner = text.mid(1, text.size() - 2);
        inner.replace(QStringLiteral("'\\''"), QStringLiteral("'"));
        return inner;
    }

    if (text.startsWith('"') && text.endsWith('"') && text.size() >= 2) {
        QString inner = text.mid(1, text.size() - 2);
        QString result;
        result.reserve(inner.size());
        bool escaping = false;
        for (const QChar ch : inner) {
            if (escaping) {
                result.append(ch);
                escaping = false;
            } else if (ch == '\\') {
                escaping = true;
            } else {
                result.append(ch);
            }
        }
        if (escaping) {
            result.append('\\');
        }
        return result;
    }

    return text;
}

QString EnvFile::formatAssignment(const QString &key, const QString &value) {
    return key + QStringLiteral("=") + (isBareValueSafe(value) ? value : shellQuote(value));
}

bool EnvFile::isBareValueSafe(const QString &value) {
    if (value.isEmpty()) {
        return true;
    }
    static const QRegularExpression safe(QStringLiteral("^[A-Za-z0-9_./:@%+=,-]+$"));
    return safe.match(value).hasMatch();
}

QString EnvFile::shellQuote(const QString &value) {
    QString quoted = value;
    quoted.replace(QStringLiteral("'"), QStringLiteral("'\\''"));
    return QStringLiteral("'") + quoted + QStringLiteral("'");
}
