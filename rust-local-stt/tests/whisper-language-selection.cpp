// Model-free regression for the helper used by both native decoder paths.
#include "arch/whisper/language-selection.h"
#include "transcribe/whisper.h"

#include <cassert>
#include <cstddef>
#include <iostream>

using namespace transcribe::whisper;

int main() {
    static_assert(sizeof(transcribe_whisper_run_ext) == 80);
    static_assert(offsetof(transcribe_whisper_run_ext_v2, base) == 0);
    const std::vector<std::string> codes = {"en", "ru", "ja"};
    const std::vector<int32_t> tokens = {2, 4, 6};
    std::vector<float> logits = {0, 0, 3, 0, 2, 0, 10};
    std::vector<size_t> candidates;
    assert(resolve_language_candidates(nullptr, codes, candidates) == TRANSCRIBE_OK);
    assert(best_language_candidate(tokens, candidates, logits) == 2); // unchanged default
    assert(resolve_language_candidates(" ru,en,ru ", codes, candidates) == TRANSCRIBE_OK);
    assert((candidates == std::vector<size_t>{0, 1}));
    assert(best_language_candidate(tokens, candidates, logits) == 0); // English can win
    logits[4] = 4;
    assert(best_language_candidate(tokens, candidates, logits) == 1); // Russian can win
    logits[2] = 4;
    assert(best_language_candidate(tokens, candidates, logits) == 0); // model-order tie
    assert(resolve_language_candidates("en,ja", codes, candidates) == TRANSCRIBE_OK);
    assert(best_language_candidate(tokens, candidates, logits) == 2); // no hardcoded RU/EN
    for (const auto * invalid : {"", " ", "ru,", ",en", "ru,,en"}) {
        assert(resolve_language_candidates(invalid, codes, candidates) == TRANSCRIBE_ERR_INVALID_ARG);
    }
    assert(resolve_language_candidates("fr", codes, candidates) == TRANSCRIBE_ERR_UNSUPPORTED_LANGUAGE);
    assert(resolve_language_candidates(std::string(1025, 'x').c_str(), codes, candidates) == TRANSCRIBE_ERR_INVALID_ARG);
    // A later unrestricted call gets all candidates, with no prior-call state.
    assert(resolve_language_candidates(nullptr, codes, candidates) == TRANSCRIBE_OK);
    assert(best_language_candidate(tokens, candidates, logits) == 2);
    assert(best_language_candidate({-1, 99}, {0, 1}, logits) == -1);
    std::cout << "Whisper language candidate selection passed\n";
}
