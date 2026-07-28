#include "models/ModelCatalog.h"

#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>

ModelCatalog ModelCatalog::load(const QString &path)
{
    ModelCatalog catalog;
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        catalog.error = file.errorString();
        return catalog;
    }

    QJsonParseError parseError;
    const QJsonDocument doc = QJsonDocument::fromJson(file.readAll(), &parseError);
    if (parseError.error != QJsonParseError::NoError || !doc.isObject()) {
        catalog.error = parseError.errorString();
        return catalog;
    }

    const QJsonObject root = doc.object();
    const int catalogVersion = root.value(QStringLiteral("catalog_version")).toInt();
    const int schemaVersion = root.value(QStringLiteral("schema_version")).toInt();
    const bool isV2 = catalogVersion == 2;
    if (!isV2 && schemaVersion != 1) {
        catalog.error = QStringLiteral("unsupported model catalog version");
        return catalog;
    }
    const QJsonArray models = root.value(QStringLiteral("models")).toArray();
    if (models.isEmpty()) {
        catalog.error = QStringLiteral("model catalog contains no models");
        return catalog;
    }
    for (const QJsonValue &value : models) {
        const QJsonObject object = value.toObject();
        const QJsonObject artifact = object.value(QStringLiteral("artifact")).toObject();
        LocalModel model;
        model.id = isV2 ? object.value(QStringLiteral("slug")).toString() : object.value(QStringLiteral("id")).toString();
        model.name = object.value(QStringLiteral("name")).toString();
        model.engineType = isV2 ? QStringLiteral("transcribe-cpp") : object.value(QStringLiteral("engine_type")).toString();
        model.artifactIsDirectory = !isV2 && artifact.value(QStringLiteral("is_directory")).toBool(false);
        for (const QJsonValue &language : object.value(QStringLiteral("languages")).toArray()) {
            model.languages.append(language.toString());
        }
        model.supportsLanguageSelection = isV2 ? model.languages.size() > 1
                                               : object.value(QStringLiteral("supports_language_selection")).toBool(false);
        if (!model.id.isEmpty()) {
            catalog.models.append(model);
        }
    }

    if (catalog.models.isEmpty()) {
        catalog.error = QStringLiteral("model catalog contains no valid model entries");
        return catalog;
    }
    catalog.isValid = true;
    return catalog;
}

std::optional<LocalModel> ModelCatalog::modelById(const QString &id) const
{
    for (const LocalModel &model : models) {
        if (model.id == id) {
            return model;
        }
    }
    return std::nullopt;
}
