import json
import logging
import os
import shutil
from collections import defaultdict

from .classes import Detection
from .helpers import get_settings

log = logging.getLogger(__name__)

CONFIG_PATH = os.path.expanduser('~/BirdNET-Pi/scripts/avian-verifier.json')
REVIEW_LOG = os.path.expanduser('~/BirdNET-Pi/scripts/review-labels.jsonl')
REVIEW_CANDIDATE_DIR = os.path.expanduser('~/BirdNET-Pi/scripts/review-candidates')

DEFAULT_CONFIG = {
    'enabled': True,
    'top_n': 10,
    'targets': {
        'Passer domesticus': {
            'enabled': True,
            'common': 'House Sparrow',
            'min_confidence': 0.4,
            'review_min_confidence': 0.4,
            'min_hits': 2,
            'max_promotions_per_file': 1,
        },
    },
}


def _merge_target(default_target, override_target):
    merged = dict(default_target)
    if isinstance(override_target, dict):
        merged.update(override_target)
    return merged


def load_verifier_config():
    config = {
        'enabled': DEFAULT_CONFIG['enabled'],
        'top_n': DEFAULT_CONFIG['top_n'],
        'targets': dict(DEFAULT_CONFIG['targets']),
    }
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as cfg:
                override = json.load(cfg)
            if isinstance(override, dict):
                config.update({k: v for k, v in override.items() if k != 'targets'})
                for sci_name, target in override.get('targets', {}).items():
                    config['targets'][sci_name] = _merge_target(config['targets'].get(sci_name, {}), target)
        except Exception as e:
            log.warning('Unable to read avian verifier config %s: %s', CONFIG_PATH, e)
    return config


def _review_stats():
    stats = defaultdict(lambda: {'correct': 0, 'wrong': 0, 'unsure': 0})
    if not os.path.isfile(REVIEW_LOG):
        return stats
    try:
        with open(REVIEW_LOG, 'r') as log_file:
            lines = log_file.readlines()[-500:]
    except Exception as e:
        log.warning('Unable to read review labels %s: %s', REVIEW_LOG, e)
        return stats

    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        sci_name = row.get('sci')
        verdict = row.get('verdict')
        if sci_name in stats and verdict in stats[sci_name]:
            stats[sci_name][verdict] += 1
        elif sci_name and verdict in ('correct', 'wrong', 'unsure'):
            stats[sci_name][verdict] += 1
    return stats


def _target_with_review_adjustments(sci_name, target, stats):
    adjusted = dict(target)
    if not adjusted.get('learn_from_review', True):
        return adjusted

    species_stats = stats.get(sci_name, {})
    correct = int(species_stats.get('correct', 0))
    wrong = int(species_stats.get('wrong', 0))

    trust_after = int(adjusted.get('review_correct_to_trust', 3))
    if correct >= trust_after and wrong == 0:
        adjusted['min_confidence'] = min(
            float(adjusted.get('min_confidence', 0.25)),
            float(adjusted.get('review_min_confidence', 0.18)),
        )
        adjusted['min_hits'] = min(
            int(adjusted.get('min_hits', 2)),
            int(adjusted.get('review_min_hits', 1)),
        )

    suppress_after = int(adjusted.get('review_wrong_to_suppress', 3))
    if wrong >= suppress_after and wrong > correct:
        adjusted['min_confidence'] = max(
            float(adjusted.get('min_confidence', 0.25)),
            float(adjusted.get('review_suppressed_min_confidence', 0.5)),
        )
    return adjusted


def _species_allowed(sci_name, include_list, exclude_list, predicted_species_list, whitelist_list, target):
    if sci_name not in include_list and len(include_list) != 0:
        return False, 'include list'
    if sci_name in exclude_list and len(exclude_list) != 0:
        return False, 'exclude list'
    if (
        sci_name not in predicted_species_list
        and len(predicted_species_list) != 0
        and sci_name not in whitelist_list
        and not target.get('ignore_occurrence', False)
    ):
        return False, 'species occurrence threshold'
    return True, ''


def _enabled_targets_with_review(config):
    review_stats = _review_stats()
    targets = {}
    for sci_name, target in config.get('targets', {}).items():
        if target.get('enabled', True):
            targets[sci_name] = _target_with_review_adjustments(sci_name, target, review_stats)
    return targets


def _review_candidate_matches(raw_detections, targets, top_n, cutoff):
    matches = []
    for time_slot, entries in raw_detections.items():
        for rank, entry in enumerate(entries[:top_n], start=1):
            sci_name, confidence = entry
            if sci_name not in targets:
                continue
            target = targets[sci_name]
            confidence = float(confidence)
            min_confidence = float(target.get('review_min_confidence', target.get('min_confidence', 0.4)))
            max_confidence = min(float(target.get('max_confidence', cutoff)), cutoff)
            if min_confidence <= confidence < max_confidence:
                matches.append({
                    'time_slot': time_slot,
                    'sci_name': sci_name,
                    'confidence': confidence,
                    'rank': rank,
                })
    return matches


def _prune_review_candidate_cache(config):
    max_files = max(20, int(config.get('max_review_audio_files', 300)))
    cached = []
    if not os.path.isdir(REVIEW_CANDIDATE_DIR):
        return
    for root, _, files in os.walk(REVIEW_CANDIDATE_DIR):
        for name in files:
            if name.endswith('.wav'):
                path = os.path.join(root, name)
                try:
                    cached.append((os.path.getmtime(path), path))
                except OSError:
                    continue
    cached.sort()
    for _, path in cached[:-max_files]:
        try:
            os.remove(path)
        except OSError:
            pass


def cache_review_candidate_audio(file, raw_detections):
    config = load_verifier_config()
    if not config.get('enabled', True) or not config.get('cache_review_audio', True):
        return None

    conf = get_settings()
    cutoff = conf.getfloat('CONFIDENCE')
    targets = _enabled_targets_with_review(config)
    if not targets:
        return None

    top_n = max(1, int(config.get('top_n', 10)))
    matches = _review_candidate_matches(raw_detections, targets, top_n, cutoff)
    if not matches:
        return None

    try:
        date_dir = file.file_date.strftime('%Y-%m-%d')
        dest_dir = os.path.join(REVIEW_CANDIDATE_DIR, date_dir)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(file.file_name))
        if not os.path.isfile(dest):
            shutil.copy2(file.file_name, dest)
            best = max(matches, key=lambda match: match['confidence'])
            log.info(
                'Cached review audio %s for %s at %.0f%%',
                os.path.basename(dest),
                best['sci_name'],
                best['confidence'] * 100,
            )
            _prune_review_candidate_cache(config)
        return dest
    except Exception as e:
        log.warning('Unable to cache review audio %s: %s', file.file_name, e)
        return None


def promote_low_confidence_candidates(
        file,
        raw_detections,
        names,
        predicted_species_list,
        include_list,
        exclude_list,
        whitelist_list,
        existing_detections):
    conf = get_settings()
    cutoff = conf.getfloat('CONFIDENCE')
    config = load_verifier_config()
    if not config.get('enabled', True):
        return []

    targets = _enabled_targets_with_review(config)
    if not targets:
        return []

    top_n = max(1, int(config.get('top_n', 10)))
    existing_keys = {(d.species, d.start, d.stop) for d in existing_detections}
    candidates = defaultdict(list)

    for time_slot, entries in raw_detections.items():
        try:
            start_s, stop_s = time_slot.split(';', 1)
        except ValueError:
            continue
        seen_in_slot = set()
        for rank, entry in enumerate(entries[:top_n], start=1):
            sci_name, confidence = entry
            if sci_name in seen_in_slot or sci_name not in targets:
                continue
            seen_in_slot.add(sci_name)
            confidence = float(confidence)
            target = targets[sci_name]
            min_confidence = float(target.get('min_confidence', 0.25))
            max_confidence = min(float(target.get('max_confidence', cutoff)), cutoff)
            if confidence < min_confidence or confidence >= max_confidence:
                continue

            allowed, reason = _species_allowed(
                sci_name,
                include_list,
                exclude_list,
                predicted_species_list,
                whitelist_list,
                target,
            )
            if not allowed:
                log.info('Second-pass verifier skipped %s: %s', sci_name, reason)
                continue

            candidates[sci_name].append({
                'start': start_s,
                'stop': stop_s,
                'confidence': confidence,
                'rank': rank,
            })

    promoted = []
    for sci_name, species_candidates in candidates.items():
        target = targets[sci_name]
        min_hits = int(target.get('min_hits', 2))
        if len(species_candidates) < min_hits:
            log.info(
                'Second-pass verifier heard %s %d/%d times; leaving below threshold',
                target.get('common', sci_name),
                len(species_candidates),
                min_hits,
            )
            continue

        max_promotions = max(1, int(target.get('max_promotions_per_file', 1)))
        best_candidates = sorted(species_candidates, key=lambda c: c['confidence'], reverse=True)[:max_promotions]
        for candidate in best_candidates:
            key = (sci_name, float(candidate['start']), float(candidate['stop']))
            if key in existing_keys:
                continue
            common_name = names.get(sci_name, target.get('common', sci_name))
            detection = Detection(
                file.file_date,
                candidate['start'],
                candidate['stop'],
                sci_name,
                common_name,
                candidate['confidence'],
            )
            promoted.append(detection)
            log.info(
                'Second-pass verifier promoted %s at %.0f%% after %d matching low-confidence chunks',
                common_name,
                detection.confidence_pct,
                len(species_candidates),
            )

    return promoted
