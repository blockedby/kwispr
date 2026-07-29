#pragma once

#include <QString>
#include <QStringList>
#include <QVector>
#include <optional>

struct LocalModel
{
    QString id;
    QString name;
    QString engineType;
    bool artifactIsDirectory = false;
    QStringList languages;
    bool supportsLanguageSelection = false;
    bool supportsLanguageDetection = false;
};

struct ModelCatalog
{
    bool isValid = false;
    QString error;
    QVector<LocalModel> models;

    static ModelCatalog load(const QString &path);
    std::optional<LocalModel> modelById(const QString &id) const;
};
