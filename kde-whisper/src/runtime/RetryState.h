#pragma once

#include <QString>

// Returns the WAV path from the canonical raw-path state format or the legacy
// `kwispr.sh retry "…"` command format.
QString retryWavPathFromState(QString state);
