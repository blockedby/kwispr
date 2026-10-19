// Kwispr: shared, model-free language candidate selection for serial and batch.
#pragma once

#include "transcribe.h"

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

namespace transcribe::whisper {

inline transcribe_status resolve_language_candidates(const char * allowed,
                                                     const std::vector<std::string> & codes,
                                                     std::vector<size_t> & candidates) {
    candidates.clear();
    if (allowed == nullptr) {
        for (size_t i = 0; i < codes.size(); ++i) {
            candidates.push_back(i);
        }
        return TRANSCRIBE_OK;
    }
    size_t length = 0;
    while (length <= 1024 && allowed[length] != '\0') {
        ++length;
    }
    if (length == 0 || length > 1024) {
        return TRANSCRIBE_ERR_INVALID_ARG;
    }
    const std::string csv(allowed, length);
    size_t start = 0;
    while (start <= csv.size()) {
        const size_t comma = csv.find(',', start);
        const size_t end = comma == std::string::npos ? csv.size() : comma;
        size_t first = start;
        size_t last = end;
        while (first < last && std::isspace(static_cast<unsigned char>(csv[first]))) {
            ++first;
        }
        while (last > first && std::isspace(static_cast<unsigned char>(csv[last - 1]))) {
            --last;
        }
        if (first == last) {
            return TRANSCRIBE_ERR_INVALID_ARG;
        }
        const auto found = std::find(codes.begin(), codes.end(), csv.substr(first, last - first));
        if (found == codes.end()) {
            return TRANSCRIBE_ERR_UNSUPPORTED_LANGUAGE;
        }
        const size_t index = static_cast<size_t>(found - codes.begin());
        if (std::find(candidates.begin(), candidates.end(), index) == candidates.end()) {
            candidates.push_back(index);
        }
        if (comma == std::string::npos) {
            break;
        }
        start = comma + 1;
    }
    // Preserve model ordering for equal logits, independent of CSV order.
    std::sort(candidates.begin(), candidates.end());
    return TRANSCRIBE_OK;
}

inline int best_language_candidate(const std::vector<int32_t> & token_ids,
                                   const std::vector<size_t> & candidates,
                                   const std::vector<float> & logits) {
    float best = -std::numeric_limits<float>::infinity();
    int best_index = -1;
    for (const size_t index : candidates) {
        if (index >= token_ids.size()) {
            continue;
        }
        const int32_t id = token_ids[index];
        if (id >= 0 && static_cast<size_t>(id) < logits.size() && logits[id] > best) {
            best = logits[id];
            best_index = static_cast<int>(index);
        }
    }
    return best_index;
}

}  // namespace transcribe::whisper
