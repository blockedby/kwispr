#include "ui/SettingsDialog.h"

#include "models/ModelManager.h"

#include <QtTest/QtTest>
#include <QAbstractButton>
#include <QCheckBox>
#include <QComboBox>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QGroupBox>
#include <QLabel>
#include <QLayout>
#include <QLineEdit>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QSignalSpy>
#include <QTimer>

class FakeModelManager : public ModelManager
{
public:
    explicit FakeModelManager(QObject *parent = nullptr)
        : ModelManager(QStringLiteral("/repo"), QStringLiteral("/catalog"), QStringLiteral("/models"), nullptr, parent)
    {
    }

    bool nextSuccess = true;
    bool autoComplete = true;
    QString nextStdout;
    QString nextStderr;
    QString lastOperation;
    QString lastModelId;
    bool fakeBusy = false;

    void complete()
    {
        if (!fakeBusy) {
            return;
        }
        fakeBusy = false;
        emit operationFinished(lastOperation, lastModelId, nextSuccess, nextStdout, nextStderr);
    }

    bool startDownload(const QString &modelId) override
    {
        return startFake(QStringLiteral("download"), modelId);
    }

    bool startDelete(const QString &modelId) override
    {
        return startFake(QStringLiteral("delete"), modelId);
    }

private:
    bool startFake(const QString &operation, const QString &modelId)
    {
        if (fakeBusy) {
            return false;
        }
        fakeBusy = true;
        lastOperation = operation;
        lastModelId = modelId;
        emit operationStarted(operation, modelId);
        if (autoComplete) {
            QTimer::singleShot(20, this, [this]() {
                complete();
            });
        }
        return true;
    }
};

class SettingsDialogTest : public QObject {
    Q_OBJECT
private slots:
    void loadsCurrentSettingsIntoWidgets();
    void backendRowsAreContextSensitiveAndPreserveDrafts();
    void apiKeyIsPasswordAndValidationDoesNotLeakSecret();
    void localModelComboDrivesSavedModel();
    void unmatchedLocalModelRoundTripsButCannotBeManaged();
    void localSavePreservesHiddenApiKeyAcrossBackendDrafts();
    void localSaveRejectsInvalidEmptyOrUnselectedCatalog();
    void modelComboAndActionsReflectInstallState();
    void vadControlsFollowEnabledStateAndProvider();
    void deletingConfiguredLocalModelShowsAdditionalWarning();
    void asynchronousModelOperationsUpdateState();
    void busyModelOperationBlocksSaveAndClosingUntilCompletion();
};

static ModelCatalog sampleCatalog()
{
    ModelCatalog catalog;
    catalog.isValid = true;
    catalog.models.append(LocalModel{QStringLiteral("whisper-large-v3-turbo"), QStringLiteral("Large v3 Turbo"), QStringLiteral("whisper.cpp"), false, {QStringLiteral("en"), QStringLiteral("ru")}, true});
    catalog.models.append(LocalModel{QStringLiteral("parakeet-tdt"), QStringLiteral("Parakeet TDT"), QStringLiteral("parakeet"), true, {}, false});
    return catalog;
}

static KwisprSettings localSettings()
{
    KwisprSettings settings;
    settings.applyLocalPreset(QStringLiteral("whisper-large-v3-turbo"), QStringLiteral("/tmp/models"), QStringLiteral("en"));
    return settings;
}

void SettingsDialogTest::loadsCurrentSettingsIntoWidgets()
{
    KwisprSettings settings;
    settings.backend = QStringLiteral("openrouter-chat");
    settings.apiUrl = QStringLiteral("https://openrouter.ai/api/v1/chat/completions");
    settings.apiKey = QStringLiteral("secret-key");
    settings.model = QStringLiteral("openai/gpt-4o-mini-transcribe");
    settings.language = QStringLiteral("ru");
    settings.transcriptionPrompt = QStringLiteral("Clean punctuation only");
    settings.autopaste = false;
    settings.pasteHotkey = QStringLiteral("ctrl-shift-v");
    settings.autopasteDelay = 0.75;
    settings.vadEnabled = true;
    settings.vadProvider = QStringLiteral("silero");
    settings.vadModelPath = QStringLiteral("/models/silero.onnx");

    SettingsDialog dialog(settings, sampleCatalog(), {QStringLiteral("whisper-large-v3-turbo")});

    QCOMPARE(dialog.findChild<QComboBox *>("backendCombo")->currentText(), QStringLiteral("OpenRouter"));
    QCOMPARE(dialog.findChild<QLineEdit *>("apiUrlEdit")->text(), settings.apiUrl);
    QCOMPARE(dialog.findChild<QLineEdit *>("apiKeyEdit")->text(), settings.apiKey);
    QCOMPARE(dialog.findChild<QLineEdit *>("modelEdit")->text(), settings.model);
    QCOMPARE(dialog.findChild<QComboBox *>("localModelCombo")->findData(settings.model), -1);
    QCOMPARE(dialog.findChild<QLineEdit *>("languageEdit")->text(), settings.language);
    QCOMPARE(dialog.findChild<QPlainTextEdit *>("promptEdit")->toPlainText(), settings.transcriptionPrompt);
    QVERIFY(!dialog.findChild<QCheckBox *>("autopasteCheck")->isChecked());
    QCOMPARE(dialog.findChild<QComboBox *>("pasteHotkeyCombo")->currentText(), settings.pasteHotkey);
    QCOMPARE(dialog.findChild<QDoubleSpinBox *>("autopasteDelaySpin")->value(), settings.autopasteDelay);
    QVERIFY(dialog.findChild<QCheckBox *>("vadEnabledCheck")->isChecked());
}

void SettingsDialogTest::backendRowsAreContextSensitiveAndPreserveDrafts()
{
    KwisprSettings settings;
    settings.apiKey = QStringLiteral("remember-this-key");
    SettingsDialog dialog(settings, sampleCatalog(), {});
    dialog.show();
    QTest::qWait(1);

    auto *backend = dialog.findChild<QComboBox *>("backendCombo");
    auto *localModels = dialog.findChild<QComboBox *>("localModelCombo");
    QCOMPARE(localModels->findData(QStringLiteral("whisper-1")), -1);
    auto visible = [&dialog](const char *name) {
        return dialog.findChild<QWidget *>(name)->isVisible();
    };
    auto verifyRow = [&visible](const char *field, const char *label, bool expected) {
        QCOMPARE(visible(field), expected);
        QCOMPARE(visible(label), expected);
    };

    backend->setCurrentText(QStringLiteral("Local STT"));
    verifyRow("apiUrlEdit", "apiUrlLabel", true);
    verifyRow("apiKeyEdit", "apiKeyLabel", false);
    verifyRow("modelEdit", "modelLabel", false);
    verifyRow("localModelRow", "localModelLabel", true);
    verifyRow("languageEdit", "languageLabel", true);
    verifyRow("promptEdit", "promptLabel", false);
    QVERIFY(visible("vadGroup"));
    QVERIFY(visible("pasteGroup"));
    auto *apiUrl = dialog.findChild<QLineEdit *>("apiUrlEdit");
    QCOMPARE(apiUrl->text(), QStringLiteral("http://127.0.0.1:9000/v1/audio/transcriptions"));
    QVERIFY(apiUrl->isReadOnly());

    localModels->setCurrentIndex(localModels->findData(QStringLiteral("parakeet-tdt")));
    verifyRow("languageEdit", "languageLabel", false);
    localModels->setCurrentIndex(localModels->findData(QStringLiteral("whisper-large-v3-turbo")));
    verifyRow("languageEdit", "languageLabel", true);

    backend->setCurrentText(QStringLiteral("OpenAI"));
    verifyRow("apiUrlEdit", "apiUrlLabel", true);
    verifyRow("apiKeyEdit", "apiKeyLabel", true);
    verifyRow("modelEdit", "modelLabel", true);
    verifyRow("localModelRow", "localModelLabel", false);
    verifyRow("languageEdit", "languageLabel", true);
    verifyRow("promptEdit", "promptLabel", false);
    QVERIFY(!visible("vadGroup"));
    QVERIFY(visible("pasteGroup"));
    QCOMPARE(dialog.findChild<QLineEdit *>("apiKeyEdit")->text(), QStringLiteral("remember-this-key"));
    QVERIFY(!apiUrl->isReadOnly());
    dialog.findChild<QLineEdit *>("modelEdit")->setText(QStringLiteral("custom-openai-model"));

    backend->setCurrentText(QStringLiteral("OpenRouter"));
    verifyRow("apiUrlEdit", "apiUrlLabel", true);
    verifyRow("apiKeyEdit", "apiKeyLabel", true);
    verifyRow("modelEdit", "modelLabel", true);
    verifyRow("localModelRow", "localModelLabel", false);
    verifyRow("languageEdit", "languageLabel", false);
    verifyRow("promptEdit", "promptLabel", true);
    QVERIFY(!visible("vadGroup"));
    QVERIFY(visible("pasteGroup"));
    QCOMPARE(dialog.findChild<QLineEdit *>("apiUrlEdit")->text(), QStringLiteral("https://openrouter.ai/api/v1/chat/completions"));
    QVERIFY(!apiUrl->isReadOnly());
    QCOMPARE(localModels->findData(QStringLiteral("openai/gpt-4o-mini-transcribe")), -1);

    backend->setCurrentText(QStringLiteral("OpenAI"));
    QCOMPARE(dialog.findChild<QLineEdit *>("modelEdit")->text(), QStringLiteral("custom-openai-model"));
}

void SettingsDialogTest::apiKeyIsPasswordAndValidationDoesNotLeakSecret()
{
    KwisprSettings settings;
    settings.apiUrl = QStringLiteral("https://api.openai.com/v1/audio/transcriptions");
    settings.apiKey = QStringLiteral("super-secret-token");
    settings.pasteHotkey = QStringLiteral("bad-hotkey");
    SettingsDialog dialog(settings, sampleCatalog(), {});

    QCOMPARE(dialog.findChild<QLineEdit *>("apiKeyEdit")->echoMode(), QLineEdit::Password);
    QVERIFY(!dialog.save());
    QVERIFY(!dialog.lastError().contains(QStringLiteral("super-secret-token")));
    QVERIFY(dialog.lastError().contains(QStringLiteral("Unsupported paste hotkey")));
}

void SettingsDialogTest::localModelComboDrivesSavedModel()
{
    KwisprSettings settings = localSettings();
    EnvFile env;
    SettingsDialog dialog(settings, sampleCatalog(), {QStringLiteral("whisper-large-v3-turbo")}, &env);

    auto *models = dialog.findChild<QComboBox *>("localModelCombo");
    models->setCurrentIndex(1);
    dialog.findChild<QLineEdit *>("modelEdit")->setText(QStringLiteral("wrong-generic-model"));

    QVERIFY(dialog.save());
    QCOMPARE(dialog.currentSettings().model, QStringLiteral("parakeet-tdt"));
    QCOMPARE(env.value("KWISPR_MODEL"), QStringLiteral("parakeet-tdt"));
}

void SettingsDialogTest::unmatchedLocalModelRoundTripsButCannotBeManaged()
{
    KwisprSettings settings = localSettings();
    settings.model = QStringLiteral("legacy/custom-local-model");
    EnvFile env;
    FakeModelManager manager;
    SettingsDialog dialog(settings, sampleCatalog(), {}, &env, &manager);
    dialog.show();
    QTest::qWait(1);

    auto *models = dialog.findChild<QComboBox *>("localModelCombo");
    auto *download = dialog.findChild<QPushButton *>("localModelDownloadButton");
    auto *remove = dialog.findChild<QPushButton *>("localModelDeleteButton");
    QCOMPARE(models->count(), 3);
    QCOMPARE(models->currentData().toString(), settings.model);
    QCOMPARE(models->currentText(), QStringLiteral("legacy/custom-local-model (not in catalog)"));
    QVERIFY(!download->isEnabled());
    QVERIFY(!remove->isEnabled());
    QVERIFY(dialog.findChild<QLineEdit *>("languageEdit")->isVisible());
    QVERIFY(dialog.findChild<QLabel *>("languageLabel")->isVisible());

    QVERIFY(dialog.save());
    QCOMPARE(dialog.currentSettings().model, settings.model);
    QCOMPARE(env.value("KWISPR_MODEL"), settings.model);
}

void SettingsDialogTest::localSavePreservesHiddenApiKeyAcrossBackendDrafts()
{
    KwisprSettings settings = localSettings();
    EnvFile env;
    env.setValue(QStringLiteral("KWISPR_API_KEY"), QStringLiteral("existing-hidden-key"));
    SettingsDialog dialog(settings, sampleCatalog(), {}, &env);

    auto *backend = dialog.findChild<QComboBox *>("backendCombo");
    auto *apiKey = dialog.findChild<QLineEdit *>("apiKeyEdit");
    QVERIFY(apiKey->isHidden());
    backend->setCurrentText(QStringLiteral("OpenAI"));
    QCOMPARE(apiKey->text(), QStringLiteral("existing-hidden-key"));
    apiKey->setText(QStringLiteral("unsaved-openai-draft"));
    backend->setCurrentText(QStringLiteral("Local STT"));
    QCOMPARE(apiKey->text(), QStringLiteral("existing-hidden-key"));

    QVERIFY(dialog.save());
    QCOMPARE(env.value("KWISPR_API_KEY"), QStringLiteral("existing-hidden-key"));
    QCOMPARE(dialog.currentSettings().apiKey, QStringLiteral("existing-hidden-key"));
}

void SettingsDialogTest::localSaveRejectsInvalidEmptyOrUnselectedCatalog()
{
    ModelCatalog invalidCatalog;
    invalidCatalog.error = QStringLiteral("catalog parse failed");
    SettingsDialog invalidDialog(localSettings(), invalidCatalog, {});
    QVERIFY(!invalidDialog.save());
    QVERIFY(invalidDialog.lastError().contains(QStringLiteral("invalid or empty")));
    QVERIFY(invalidDialog.lastError().contains(QStringLiteral("catalog parse failed")));

    ModelCatalog emptyCatalog;
    emptyCatalog.isValid = true;
    SettingsDialog emptyDialog(localSettings(), emptyCatalog, {});
    QVERIFY(!emptyDialog.save());
    QVERIFY(emptyDialog.lastError().contains(QStringLiteral("invalid or empty")));

    KwisprSettings noModelSettings = localSettings();
    noModelSettings.model.clear();
    SettingsDialog noModelDialog(noModelSettings, sampleCatalog(), {});
    QCOMPARE(noModelDialog.findChild<QComboBox *>("localModelCombo")->currentIndex(), -1);
    QVERIFY(!noModelDialog.save());
    QVERIFY(noModelDialog.lastError().contains(QStringLiteral("Select a local model")));
}

void SettingsDialogTest::modelComboAndActionsReflectInstallState()
{
    FakeModelManager manager;
    SettingsDialog dialog(localSettings(), sampleCatalog(), {QStringLiteral("whisper-large-v3-turbo")}, nullptr, &manager);

    auto *models = dialog.findChild<QComboBox *>("localModelCombo");
    auto *download = dialog.findChild<QPushButton *>("localModelDownloadButton");
    auto *remove = dialog.findChild<QPushButton *>("localModelDeleteButton");
    auto *row = dialog.findChild<QWidget *>("localModelRow");
    QCOMPARE(row->layout()->count(), 2);
    QVERIFY(row->layout()->itemAt(0)->layout());
    QVERIFY(row->layout()->itemAt(1)->layout());
    QCOMPARE(models->count(), 2);
    QCOMPARE(models->itemText(0), QStringLiteral("Large v3 Turbo (installed)"));
    QCOMPARE(models->itemData(0).toString(), QStringLiteral("whisper-large-v3-turbo"));
    QCOMPARE(models->itemText(1), QStringLiteral("Parakeet TDT (not installed)"));
    QVERIFY(!download->isEnabled());
    QVERIFY(remove->isEnabled());

    models->setCurrentIndex(1);
    QVERIFY(download->isEnabled());
    QVERIFY(!remove->isEnabled());
}

void SettingsDialogTest::vadControlsFollowEnabledStateAndProvider()
{
    KwisprSettings settings = localSettings();
    settings.vadEnabled = false;
    settings.vadProvider = QStringLiteral("energy");
    settings.vadModelPath = QStringLiteral("/models/preserved-silero.onnx");
    SettingsDialog dialog(settings, sampleCatalog(), {});
    dialog.show();
    QTest::qWait(1);

    auto *enabled = dialog.findChild<QCheckBox *>("vadEnabledCheck");
    auto *provider = dialog.findChild<QComboBox *>("vadProviderCombo");
    auto *modelPath = dialog.findChild<QLineEdit *>("vadModelPathEdit");
    auto *modelPathLabel = dialog.findChild<QLabel *>("vadModelPathLabel");
    auto *threshold = dialog.findChild<QDoubleSpinBox *>("vadThresholdSpin");
    auto *frame = dialog.findChild<QLineEdit *>("vadFrameMsEdit");

    QVERIFY(!provider->isEnabled());
    QVERIFY(!threshold->isEnabled());
    QVERIFY(!frame->isEnabled());
    QVERIFY(!modelPath->isVisible());
    QVERIFY(!modelPathLabel->isVisible());
    QVERIFY(!modelPath->isEnabled());

    enabled->setChecked(true);
    QVERIFY(provider->isEnabled());
    QVERIFY(threshold->isEnabled());
    QVERIFY(frame->isEnabled());
    QVERIFY(!modelPath->isVisible());

    provider->setCurrentText(QStringLiteral("silero"));
    QVERIFY(modelPath->isVisible());
    QVERIFY(modelPathLabel->isVisible());
    QVERIFY(modelPath->isEnabled());
    QCOMPARE(modelPath->text(), QStringLiteral("/models/preserved-silero.onnx"));

    enabled->setChecked(false);
    QVERIFY(!provider->isEnabled());
    QVERIFY(!threshold->isEnabled());
    QVERIFY(!frame->isEnabled());
    QVERIFY(!modelPath->isVisible());
    QVERIFY(!modelPath->isEnabled());
    QCOMPARE(modelPath->text(), QStringLiteral("/models/preserved-silero.onnx"));
}

void SettingsDialogTest::deletingConfiguredLocalModelShowsAdditionalWarning()
{
    FakeModelManager manager;
    SettingsDialog dialog(localSettings(), sampleCatalog(), {QStringLiteral("whisper-large-v3-turbo")}, nullptr, &manager);
    dialog.show();

    QString confirmationText;
    QTimer::singleShot(10, [&confirmationText]() {
        for (QWidget *widget : QApplication::topLevelWidgets()) {
            if (auto *box = qobject_cast<QMessageBox *>(widget)) {
                confirmationText = box->text();
                box->button(QMessageBox::No)->click();
                return;
            }
        }
    });
    QTest::mouseClick(dialog.findChild<QPushButton *>("localModelDeleteButton"), Qt::LeftButton);

    QVERIFY(confirmationText.contains(QStringLiteral("currently configured Local STT model")));
    QVERIFY(confirmationText.contains(QStringLiteral("select and apply another installed model")));
    QVERIFY(confirmationText.contains(QStringLiteral("future server starts")));
    QVERIFY(manager.lastOperation.isEmpty());
}

void SettingsDialogTest::asynchronousModelOperationsUpdateState()
{
    FakeModelManager manager;
    SettingsDialog dialog(localSettings(), sampleCatalog(), {QStringLiteral("whisper-large-v3-turbo")}, nullptr, &manager);
    dialog.show();
    auto *models = dialog.findChild<QComboBox *>("localModelCombo");
    auto *download = dialog.findChild<QPushButton *>("localModelDownloadButton");
    auto *remove = dialog.findChild<QPushButton *>("localModelDeleteButton");
    auto *busy = dialog.findChild<QProgressBar *>("localModelBusyIndicator");
    auto *status = dialog.findChild<QLabel *>("modelOperationStatusLabel");
    models->setCurrentIndex(1);

    QTest::mouseClick(download, Qt::LeftButton);
    QCOMPARE(manager.lastOperation, QStringLiteral("download"));
    QCOMPARE(manager.lastModelId, QStringLiteral("parakeet-tdt"));
    QVERIFY(!models->isEnabled());
    QVERIFY(!download->isEnabled());
    QVERIFY(!remove->isEnabled());
    QVERIFY(busy->isVisible());
    QTRY_COMPARE(models->itemText(1), QStringLiteral("Parakeet TDT (installed)"));
    QVERIFY(!download->isEnabled());
    QVERIFY(remove->isEnabled());
    QVERIFY(status->text().contains(QStringLiteral("Downloaded Parakeet TDT")));

    QTimer::singleShot(10, []() {
        for (QWidget *widget : QApplication::topLevelWidgets()) {
            if (auto *box = qobject_cast<QMessageBox *>(widget)) {
                box->button(QMessageBox::Yes)->click();
                return;
            }
        }
    });
    QTest::mouseClick(remove, Qt::LeftButton);
    QCOMPARE(manager.lastOperation, QStringLiteral("delete"));
    QCOMPARE(manager.lastModelId, QStringLiteral("parakeet-tdt"));
    QTRY_COMPARE(models->itemText(1), QStringLiteral("Parakeet TDT (not installed)"));
    QVERIFY(download->isEnabled());
    QVERIFY(!remove->isEnabled());
    QVERIFY(status->text().contains(QStringLiteral("Deleted Parakeet TDT")));

    manager.nextSuccess = false;
    manager.nextStderr = QStringLiteral("simulated helper failure");
    QTest::mouseClick(download, Qt::LeftButton);
    QVERIFY(!models->isEnabled());
    QTRY_VERIFY(status->text().contains(QStringLiteral("Download failed: simulated helper failure")));
    QCOMPARE(models->itemText(1), QStringLiteral("Parakeet TDT (not installed)"));
    QVERIFY(download->isEnabled());
    QVERIFY(!remove->isEnabled());
}

void SettingsDialogTest::busyModelOperationBlocksSaveAndClosingUntilCompletion()
{
    FakeModelManager manager;
    manager.autoComplete = false;
    EnvFile env;
    SettingsDialog dialog(localSettings(), sampleCatalog(), {QStringLiteral("whisper-large-v3-turbo")}, &env, &manager);
    dialog.show();
    QSignalSpy rejectedSpy(&dialog, &QDialog::rejected);

    auto *models = dialog.findChild<QComboBox *>("localModelCombo");
    auto *download = dialog.findChild<QPushButton *>("localModelDownloadButton");
    auto *remove = dialog.findChild<QPushButton *>("localModelDeleteButton");
    auto *buttons = dialog.findChild<QDialogButtonBox *>("buttonBox");
    auto *status = dialog.findChild<QLabel *>("modelOperationStatusLabel");
    models->setCurrentIndex(1);
    QTest::mouseClick(download, Qt::LeftButton);

    QVERIFY(manager.fakeBusy);
    QVERIFY(!models->isEnabled());
    QVERIFY(!download->isEnabled());
    QVERIFY(!remove->isEnabled());
    QVERIFY(!buttons->button(QDialogButtonBox::Ok)->isEnabled());
    QVERIFY(!buttons->button(QDialogButtonBox::Apply)->isEnabled());
    QVERIFY(!buttons->button(QDialogButtonBox::Cancel)->isEnabled());
    QVERIFY(!dialog.save());
    QVERIFY(dialog.lastError().contains(QStringLiteral("finish before saving")));

    dialog.reject();
    QVERIFY(dialog.isVisible());
    QCOMPARE(rejectedSpy.count(), 0);
    dialog.close();
    QVERIFY(dialog.isVisible());
    QCOMPARE(rejectedSpy.count(), 0);
    QVERIFY(status->text().contains(QStringLiteral("finish before closing")));

    manager.complete();
    QTRY_VERIFY(!manager.fakeBusy);
    QVERIFY(models->isEnabled());
    QVERIFY(buttons->button(QDialogButtonBox::Ok)->isEnabled());
    QVERIFY(buttons->button(QDialogButtonBox::Apply)->isEnabled());
    QVERIFY(buttons->button(QDialogButtonBox::Cancel)->isEnabled());
    QVERIFY(remove->isEnabled());
    QVERIFY(status->text().contains(QStringLiteral("Downloaded Parakeet TDT")));
    QVERIFY(dialog.save());

    dialog.reject();
    QCOMPARE(rejectedSpy.count(), 1);
    QVERIFY(!dialog.isVisible());
}

QTEST_MAIN(SettingsDialogTest)
#include "settings_dialog_test.moc"
