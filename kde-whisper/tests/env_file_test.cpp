#include <QtTest/QtTest>
#include <QTemporaryDir>
#include <QFile>
#include <QFileInfo>
#include <QRegularExpression>
#include "config/EnvFile.h"

class EnvFileTest : public QObject {
    Q_OBJECT
private slots:
    void loadsEnvSyntaxAndDefaults();
    void updatesWhilePreservingUserContent();
    void appendsMissingKeysAndQuotesSafely();
    void savesWithPrivatePermissions();
};

static void writeText(const QString &path, const QString &text) {
    QFile file(path);
    QVERIFY(file.open(QIODevice::WriteOnly | QIODevice::Text));
    QCOMPARE(file.write(text.toUtf8()), qsizetype(text.toUtf8().size()));
}

void EnvFileTest::loadsEnvSyntaxAndDefaults() {
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(".env");
    writeText(path,
        "# Kwispr settings\n"
        "KWISPR_BACKEND=openrouter-chat\n"
        "KWISPR_API_KEY=\n"
        "KWISPR_MODEL=\"openai/gpt-4o-mini-transcribe\"\n"
        "KWISPR_TRANSCRIPTION_PROMPT='Keep punctuation and mixed Russian English.'\n"
        "UNKNOWN_KEY=value with spaces\n"
    );

    EnvFile env;
    QVERIFY2(env.load(path), qPrintable(env.errorString()));
    QCOMPARE(env.value("KWISPR_BACKEND"), QString("openrouter-chat"));
    QCOMPARE(env.value("KWISPR_API_KEY"), QString(""));
    QCOMPARE(env.value("KWISPR_MODEL"), QString("openai/gpt-4o-mini-transcribe"));
    QCOMPARE(env.value("KWISPR_TRANSCRIPTION_PROMPT"), QString("Keep punctuation and mixed Russian English."));
    QCOMPARE(env.value("UNKNOWN_KEY"), QString("value with spaces"));

    const auto defaults = EnvFile::defaults();
    QCOMPARE(defaults.value("KWISPR_BACKEND"), QString("openai-transcriptions"));
    QCOMPARE(defaults.value("KWISPR_API_URL"), QString("https://api.openai.com/v1/audio/transcriptions"));
    QCOMPARE(defaults.value("KWISPR_LOCAL_STT_HOST"), QString("127.0.0.1"));
    QCOMPARE(defaults.value("KWISPR_LOCAL_STT_PORT"), QString("19650"));
    QCOMPARE(defaults.value("KWISPR_MODEL"), QString("whisper-1"));
    QCOMPARE(defaults.value("KWISPR_AUDIO_FORMAT"), QString("wav"));
    QCOMPARE(defaults.value("KWISPR_PULSE_SOURCE"), QString("default"));
    QCOMPARE(defaults.value("KWISPR_AUTOPASTE"), QString("1"));
    QCOMPARE(defaults.value("KWISPR_SOUNDS"), QString("1"));
    QCOMPARE(defaults.value("KWISPR_LANGUAGE"), QString(""));
    QVERIFY(defaults.contains("KWISPR_TRANSCRIPTION_PROMPT"));
}

void EnvFileTest::updatesWhilePreservingUserContent() {
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(".env");
    writeText(path,
        "# before\n"
        "KWISPR_MODEL=whisper-1\n"
        "\n"
        "CUSTOM_FLAG=yes\n"
        "# after\n"
    );

    EnvFile env;
    QVERIFY2(env.load(path), qPrintable(env.errorString()));
    env.setValue("KWISPR_MODEL", "whisper-large-v3-turbo");
    QVERIFY2(env.save(path), qPrintable(env.errorString()));

    QFile file(path);
    QVERIFY(file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QString saved = QString::fromUtf8(file.readAll());
    QVERIFY(saved.contains("# before\n"));
    QVERIFY(saved.contains("\n\nCUSTOM_FLAG=yes\n"));
    QVERIFY(saved.contains("# after\n"));
    QVERIFY(saved.contains("KWISPR_MODEL=whisper-large-v3-turbo\n"));
    QCOMPARE(saved.indexOf("# before"), 0);
    QVERIFY(saved.indexOf("CUSTOM_FLAG=yes") < saved.indexOf("# after"));
}

void EnvFileTest::appendsMissingKeysAndQuotesSafely() {
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(".env");
    writeText(path, "KWISPR_BACKEND=openai-transcriptions\n");

    EnvFile env;
    QVERIFY2(env.load(path), qPrintable(env.errorString()));
    env.setValue("KWISPR_TRANSCRIPTION_PROMPT", "Don't expand $HOME or `run`; keep \"quotes\".");
    env.setValue("KWISPR_LANGUAGE", "");
    QVERIFY2(env.save(path), qPrintable(env.errorString()));

    QFile file(path);
    QVERIFY(file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QString saved = QString::fromUtf8(file.readAll());
    QVERIFY(saved.contains("KWISPR_BACKEND=openai-transcriptions\n"));
    QVERIFY(saved.contains("KWISPR_LANGUAGE=\n"));
    QVERIFY(saved.contains("KWISPR_TRANSCRIPTION_PROMPT='Don'\\''t expand $HOME or `run`; keep \"quotes\".'\n"));

    EnvFile reloaded;
    QVERIFY2(reloaded.load(path), qPrintable(reloaded.errorString()));
    QCOMPARE(reloaded.value("KWISPR_TRANSCRIPTION_PROMPT"), QString("Don't expand $HOME or `run`; keep \"quotes\"."));
}

void EnvFileTest::savesWithPrivatePermissions() {
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(".env");
    EnvFile env;
    env.setValue("KWISPR_API_KEY", "secret");
    QVERIFY2(env.save(path), qPrintable(env.errorString()));

    QFileInfo info(path);
    QVERIFY(info.exists());
    const auto perms = info.permissions();
    QVERIFY(perms & QFileDevice::ReadOwner);
    QVERIFY(perms & QFileDevice::WriteOwner);
    QVERIFY(!(perms & QFileDevice::ReadGroup));
    QVERIFY(!(perms & QFileDevice::WriteGroup));
    QVERIFY(!(perms & QFileDevice::ReadOther));
    QVERIFY(!(perms & QFileDevice::WriteOther));
}

QTEST_MAIN(EnvFileTest)
#include "env_file_test.moc"
