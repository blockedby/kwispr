#include <QtTest/QtTest>
#include <QTemporaryDir>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QJsonValue>

#include "models/ModelCatalog.h"
#include "models/ModelManager.h"
#include "runtime/ProcessRunner.h"

class FakeRunner : public ProcessRunner
{
public:
    ProcessResult nextResult{0, {}, {}};
    QString program;
    QStringList arguments;
    QProcessEnvironment environment;

    ProcessResult run(const QString &p, const QStringList &args, const QProcessEnvironment &env) override
    {
        program = p;
        arguments = args;
        environment = env;
        return nextResult;
    }
};

class ModelManagerTest : public QObject
{
    Q_OBJECT

private slots:
    void parsesCatalogMetadata()
    {
        const QString catalogPath = QStringLiteral("%1/models/local-stt-catalog.json").arg(QDir::currentPath());
        const auto catalog = ModelCatalog::load(catalogPath);
        QVERIFY2(catalog.isValid, qPrintable(catalog.error));
        QVERIFY(catalog.models.size() >= 3);

        const auto parakeet = catalog.modelById(QStringLiteral("parakeet-tdt-0.6b-v3"));
        QVERIFY(parakeet.has_value());
        QCOMPARE(parakeet->id, QStringLiteral("parakeet-tdt-0.6b-v3"));
        QCOMPARE(parakeet->name, QStringLiteral("Parakeet TDT 0.6B v3"));
        QCOMPARE(parakeet->engineType, QStringLiteral("transcribe-cpp"));
        QVERIFY(!parakeet->artifactIsDirectory);
        QVERIFY(parakeet->languages.contains(QStringLiteral("en")));
        QVERIFY(parakeet->supportsLanguageSelection);
        QVERIFY(parakeet->supportsLanguageDetection);

        const auto gigaam = catalog.modelById(QStringLiteral("gigaam-v3-e2e-ctc"));
        QVERIFY(gigaam.has_value());
        QVERIFY(!gigaam->supportsLanguageSelection);
        QVERIFY(!gigaam->supportsLanguageDetection);

        const auto whisper = catalog.modelById(QStringLiteral("whisper-large-v3-turbo"));
        QVERIFY(whisper.has_value());
        QVERIFY(!whisper->artifactIsDirectory);
        QVERIFY(whisper->supportsLanguageSelection);
        QVERIFY(whisper->supportsLanguageDetection);
    }

    void supportsLegacyCatalogAndRejectsUnknownOrEmptyVersions()
    {
        QTemporaryDir directory;
        QVERIFY(directory.isValid());
        const QString path = directory.filePath(QStringLiteral("catalog.json"));
        auto writeCatalog = [&path](const QJsonObject &root) {
            QFile file(path);
            if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
                return false;
            }
            return file.write(QJsonDocument(root).toJson()) > 0;
        };

        const QJsonObject legacyModel{
            {QStringLiteral("id"), QStringLiteral("legacy-model")},
            {QStringLiteral("name"), QStringLiteral("Legacy Model")},
            {QStringLiteral("engine_type"), QStringLiteral("whisper")},
            {QStringLiteral("artifact"), QJsonObject{{QStringLiteral("is_directory"), false}}},
            {QStringLiteral("supports_language_selection"), true},
            {QStringLiteral("languages"), QJsonArray{QStringLiteral("en"), QStringLiteral("fr")}},
        };
        QVERIFY(writeCatalog(QJsonObject{
            {QStringLiteral("schema_version"), 1},
            {QStringLiteral("models"), QJsonArray{legacyModel}},
        }));
        const auto legacy = ModelCatalog::load(path);
        QVERIFY2(legacy.isValid, qPrintable(legacy.error));
        QCOMPARE(legacy.models.size(), 1);
        QCOMPARE(legacy.models.constFirst().id, QStringLiteral("legacy-model"));
        QVERIFY(legacy.models.constFirst().supportsLanguageSelection);
        QVERIFY(!legacy.models.constFirst().supportsLanguageDetection);

        QVERIFY(writeCatalog(QJsonObject{
            {QStringLiteral("catalog_version"), 3},
            {QStringLiteral("models"), QJsonArray{legacyModel}},
        }));
        const auto future = ModelCatalog::load(path);
        QVERIFY(!future.isValid);
        QVERIFY(future.error.contains(QStringLiteral("unsupported")));

        QVERIFY(writeCatalog(QJsonObject{
            {QStringLiteral("catalog_version"), 2},
            {QStringLiteral("models"), QJsonArray{}},
        }));
        const auto empty = ModelCatalog::load(path);
        QVERIFY(!empty.isValid);
        QVERIFY(empty.error.contains(QStringLiteral("no models")));
    }

    void parsesHelperListOutput()
    {
        const QString output = QStringLiteral(
            "gigaam-v3-e2e-ctc\tinstalled\t151 MB\tGigaAM v3\n"
            "whisper-large-v3-turbo\tnot-installed\t1550 MB\tWhisper Large v3 Turbo\n");

        const auto statuses = ModelManager::parseListOutput(output);
        QCOMPARE(statuses.size(), 2);
        QVERIFY(statuses.value(QStringLiteral("gigaam-v3-e2e-ctc")));
        QVERIFY(!statuses.value(QStringLiteral("whisper-large-v3-turbo")));
    }

    void asynchronousDownloadAndDeleteUseArgvAndEnforceSingleOperation()
    {
        const QString dangerousSlug = QStringLiteral("model; touch /tmp/never-run");
        ModelManager manager(QStringLiteral("/repo root"),
                             QStringLiteral("/catalog with spaces.json"),
                             QStringLiteral("/models with spaces"),
                             nullptr,
                             nullptr,
                             QStringLiteral("/bin/echo"));
        QSignalSpy startedSpy(&manager, &ModelManager::operationStarted);
        QSignalSpy finishedSpy(&manager, &ModelManager::operationFinished);

        QVERIFY(manager.startDownload(dangerousSlug));
        QVERIFY(manager.isBusy());
        QVERIFY(!manager.startDelete(QStringLiteral("another-model")));
        QCOMPARE(startedSpy.count(), 1);
        QCOMPARE(startedSpy.constFirst().at(0).toString(), QStringLiteral("download"));
        QTRY_COMPARE_WITH_TIMEOUT(finishedSpy.count(), 1, 3000);
        QVERIFY(!manager.isBusy());
        QVERIFY2(finishedSpy.constFirst().at(2).toBool(),
                 qPrintable(finishedSpy.constFirst().at(4).toString()));

        const QStringList downloadArgs = manager.helperArguments({QStringLiteral("download"), dangerousSlug,
                                                                  QStringLiteral("--progress"), QStringLiteral("jsonl")});
        QCOMPARE(downloadArgs, QStringList({QStringLiteral("/repo root/kwispr-models.py"),
                                            QStringLiteral("--catalog"), QStringLiteral("/catalog with spaces.json"),
                                            QStringLiteral("--model-dir"), QStringLiteral("/models with spaces"),
                                            QStringLiteral("download"), dangerousSlug,
                                            QStringLiteral("--progress"), QStringLiteral("jsonl")}));
        QCOMPARE(finishedSpy.constFirst().at(3).toString(), downloadArgs.join(QLatin1Char(' ')) + QLatin1Char('\n'));

        QVERIFY(manager.startDelete(QStringLiteral("whisper-large-v3-turbo")));
        QTRY_COMPARE_WITH_TIMEOUT(finishedSpy.count(), 2, 3000);
        const QStringList deleteArgs = manager.helperArguments({QStringLiteral("delete"), QStringLiteral("whisper-large-v3-turbo")});
        QCOMPARE(finishedSpy.at(1).at(3).toString(), deleteArgs.join(QLatin1Char(' ')) + QLatin1Char('\n'));
    }

    void downloadFramesPartialJsonlAndToleratesMalformedLines()
    {
        QTemporaryDir directory;
        QVERIFY(directory.isValid());
        const QString helperPath = directory.filePath(QStringLiteral("progress-helper.py"));
        QFile helper(helperPath);
        QVERIFY(helper.open(QIODevice::WriteOnly));
        const QByteArray source = R"SH(#!/bin/sh
printf '%s\n' 'not-json'
printf '%s\n' '{"protocol":"wrong","event":"progress","model_slug":"test-model","bytes_done":99,"bytes_total":100}'
printf '%s\n' '{"protocol":"kwispr-model-download-v1","event":"attempt","model_slug":"test-model","bytes_done":0,"bytes_total":100,"attempt":1}'
printf '%s' '{"protocol":"kwispr-model-download-v1","event":"progress","model_slug":"test-model","bytes_done":40,'
sleep 0.02
printf '%s\n' '"bytes_total":100}'
printf '%s\n' '{"protocol":"kwispr-model-download-v1","event":"reset","model_slug":"test-model","bytes_done":0,"bytes_total":100,"attempt":2}'
printf '%s\n' '{"protocol":"kwispr-model-download-v1","event":"status","model_slug":"test-model","bytes_done":100,"bytes_total":100,"phase":"checksum-verification"}'
)SH";
        QCOMPARE(helper.write(source), source.size());
        helper.close();
        QVERIFY(QFile::setPermissions(helperPath, QFileDevice::ReadUser | QFileDevice::WriteUser | QFileDevice::ExeUser));

        ModelManager manager(QStringLiteral("/repo"), QStringLiteral("/catalog"), QStringLiteral("/models"),
                             nullptr, nullptr, helperPath);
        QSignalSpy statusSpy(&manager, &ModelManager::downloadStatus);
        QSignalSpy progressSpy(&manager, &ModelManager::downloadProgress);
        QSignalSpy finishedSpy(&manager, &ModelManager::operationFinished);

        QVERIFY(manager.startDownload(QStringLiteral("test-model")));
        QTRY_COMPARE_WITH_TIMEOUT(finishedSpy.count(), 1, 3000);
        QVERIFY2(finishedSpy.constFirst().at(2).toBool(),
                 qPrintable(finishedSpy.constFirst().at(4).toString()));
        QCOMPARE(statusSpy.count(), 3);
        QCOMPARE(statusSpy.at(0).at(1).toString(), QStringLiteral("attempt"));
        QCOMPARE(statusSpy.at(1).at(1).toString(), QStringLiteral("reset"));
        QCOMPARE(statusSpy.at(2).at(1).toString(), QStringLiteral("checksum-verification"));
        QCOMPARE(statusSpy.at(2).at(2).toLongLong(), 100);
        QCOMPARE(statusSpy.at(2).at(3).toLongLong(), 100);
        QCOMPARE(progressSpy.count(), 2);
        QCOMPARE(progressSpy.at(0).at(1).toLongLong(), 40);
        QCOMPARE(progressSpy.at(0).at(2).toLongLong(), 100);
        QCOMPARE(progressSpy.at(1).at(1).toLongLong(), 0);
        QVERIFY(finishedSpy.constFirst().at(3).toString().contains(QStringLiteral("not-json")));
    }

    void asynchronousFailureCapturesStderr()
    {
        ModelManager manager(QStringLiteral("/repo"),
                             QStringLiteral("/catalog.json"),
                             QStringLiteral("/models"),
                             nullptr,
                             nullptr,
                             QStringLiteral("/definitely/missing/program"));
        QSignalSpy finishedSpy(&manager, &ModelManager::operationFinished);

        QVERIFY(manager.startDownload(QStringLiteral("test-model")));
        QTRY_COMPARE_WITH_TIMEOUT(finishedSpy.count(), 1, 3000);
        QVERIFY(!finishedSpy.constFirst().at(2).toBool());
        QVERIFY(!finishedSpy.constFirst().at(4).toString().isEmpty());
    }

    void verifyUsesHelperExitCodeAsAuthority()
    {
        FakeRunner runner;
        runner.nextResult = ProcessResult{1, QStringLiteral("whisper-large-v3-turbo: missing-or-invalid\n"), QString()};
        ModelManager manager(QStringLiteral("/repo"), QStringLiteral("/repo/models/local-stt-catalog.json"), QStringLiteral("/models"), &runner);

        const auto result = manager.verify(QStringLiteral("whisper-large-v3-turbo"));

        QCOMPARE(result.exitCode, 1);
        QCOMPARE(runner.program, QStringLiteral("python3"));
        QCOMPARE(runner.arguments.last(), QStringLiteral("whisper-large-v3-turbo"));
    }

    void listStatusDelegatesAndParses()
    {
        FakeRunner runner;
        runner.nextResult = ProcessResult{0, QStringLiteral("a\tinstalled\t1 MB\tA\n"), QString()};
        ModelManager manager(QStringLiteral("/repo"), QStringLiteral("/catalog.json"), QStringLiteral("/models"), &runner);

        const auto statuses = manager.listInstalledStatus();

        QCOMPARE(statuses.size(), 1);
        QVERIFY(statuses.value(QStringLiteral("a")));
        QCOMPARE(runner.arguments, QStringList({QStringLiteral("/repo/kwispr-models.py"),
                                                QStringLiteral("--catalog"), QStringLiteral("/catalog.json"),
                                                QStringLiteral("--model-dir"), QStringLiteral("/models"),
                                                QStringLiteral("list")}));
    }
};

QTEST_MAIN(ModelManagerTest)
#include "model_manager_test.moc"
