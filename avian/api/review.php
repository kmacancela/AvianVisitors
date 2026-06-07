<?php
// AvianVisitors - LAN-only review helper for rejected / low-confidence
// BirdNET analysis candidates. It parses recent birdnet_analysis logs,
// links each candidate to its StreamData WAV chunk, and optionally records
// simple human review marks as JSONL for later tuning.

declare(strict_types=1);
header('Cache-Control: no-store');

if (getenv('AV_REQUIRE_AUTH') === '1' && empty($_SERVER['HTTP_AUTHORIZATION'])) {
    http_response_code(401);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(['error' => 'unauthorized']);
    exit;
}

$BIRDNETPI_DIR = dirname(__DIR__, 2);
$BIRDSONGS_DIR = dirname(__DIR__, 3) . '/BirdSongs';
$STREAM_DIR    = "$BIRDSONGS_DIR/StreamData";
$REVIEW_LOG    = "$BIRDNETPI_DIR/scripts/review-labels.jsonl";
$REVIEW_FALLBACK_LOG = '/tmp/avian-review-labels.jsonl';
$REVIEW_CLIPS_DIR = "$BIRDNETPI_DIR/scripts/review-clips";
$REVIEW_FALLBACK_CLIPS_DIR = '/tmp/avian-review-clips';
$REVIEW_CANDIDATES_DIR = "$BIRDNETPI_DIR/scripts/review-candidates";
$action        = $_GET['action'] ?? 'candidates';

function review_json($data, int $status = 200): void {
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($data);
}

function review_shell(string $cmd): string {
    $rc = 0; $out = [];
    exec($cmd . ' 2>&1', $out, $rc);
    return implode("\n", $out);
}

function review_audio_file(string $file): ?string {
    global $STREAM_DIR, $REVIEW_CANDIDATES_DIR;
    $file = basename($file);
    if (!preg_match('/^\d{4}-\d{2}-\d{2}-birdnet-\d{2}:\d{2}:\d{2}\.wav$/', $file)) {
        return null;
    }
    $date = substr($file, 0, 10);
    $paths = [
        "$STREAM_DIR/$file",
        "$REVIEW_CANDIDATES_DIR/$date/$file",
        "$REVIEW_CANDIDATES_DIR/$file",
    ];
    foreach ($paths as $path) {
        if (is_file($path) && filesize($path) >= 64) return $path;
    }
    return null;
}

function safe_stream_file(string $file): ?string {
    return review_audio_file($file);
}

function species_common(string $raw): array {
    $raw = trim($raw);
    $parts = explode('_', $raw, 2);
    if (count($parts) === 2) {
        return ['sci' => trim($parts[0]), 'com' => trim($parts[1])];
    }
    return ['sci' => '', 'com' => $raw];
}

function review_slug(string $value): string {
    $value = strtolower(trim($value));
    $value = preg_replace('/[^a-z0-9]+/', '-', $value);
    $value = trim((string)$value, '-');
    return $value !== '' ? $value : 'unknown';
}

function save_review_clip(array $mark): ?string {
    global $REVIEW_CLIPS_DIR, $REVIEW_FALLBACK_CLIPS_DIR;
    $source = review_audio_file((string)$mark['file']);
    if (!$source) return null;

    $species = review_slug((string)($mark['sci'] ?: $mark['com']));
    $verdict = review_slug((string)$mark['verdict']);
    $baseName = pathinfo((string)$mark['file'], PATHINFO_FILENAME);
    $start = number_format((float)$mark['start_s'], 1, '.', '');
    $stop = number_format((float)$mark['end_s'], 1, '.', '');
    $name = $baseName . '-' . $start . '-' . $stop . '-' . review_slug((string)$mark['com']) . '.wav';

    foreach ([$REVIEW_CLIPS_DIR, $REVIEW_FALLBACK_CLIPS_DIR] as $root) {
        $dir = "$root/$verdict/$species";
        if (!is_dir($dir) && !@mkdir($dir, 0775, true)) {
            continue;
        }
        $dest = "$dir/$name";
        if (@copy($source, $dest)) {
            return $dest;
        }
    }
    return null;
}

function review_key(string $file, float $start, float $end, string $sci): string {
    return implode('|', [
        basename($file),
        number_format($start, 3, '.', ''),
        number_format($end, 3, '.', ''),
        trim($sci),
    ]);
}

function reviewed_lookup(): array {
    global $REVIEW_LOG, $REVIEW_FALLBACK_LOG;
    $out = [];
    foreach ([$REVIEW_FALLBACK_LOG, $REVIEW_LOG] as $path) {
        if (!is_file($path)) continue;
        $lines = @file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
        if (!is_array($lines)) continue;
        foreach ($lines as $line) {
            $row = json_decode($line, true);
            if (!is_array($row)) continue;
            $verdict = (string)($row['verdict'] ?? '');
            if (!preg_match('/^(correct|wrong|unsure)$/', $verdict)) continue;
            $key = review_key(
                (string)($row['file'] ?? ''),
                (float)($row['start_s'] ?? 0),
                (float)($row['end_s'] ?? 0),
                (string)($row['sci'] ?? '')
            );
            $out[$key] = [
                'verdict' => $verdict,
                'reviewed_at' => (string)($row['reviewed_at'] ?? ''),
                'clip_path' => (string)($row['clip_path'] ?? ''),
            ];
        }
    }
    return $out;
}

function accepted_lookup(): array {
    $dbPath = dirname(__DIR__, 2) . '/scripts/birds.db';
    if (!is_file($dbPath)) return [];
    try {
        $db = new SQLite3($dbPath, SQLITE3_OPEN_READONLY);
        $db->busyTimeout(1000);
        $rs = $db->query("SELECT File_Name AS file FROM detections WHERE File_Name IS NOT NULL ORDER BY Date DESC, Time DESC LIMIT 1000");
        $out = [];
        while ($row = $rs->fetchArray(SQLITE3_ASSOC)) {
            $out[(string)$row['file']] = true;
        }
        return $out;
    } catch (Throwable $e) {
        return [];
    }
}

function candidates(): array {
    $lines = max(80, min(1200, (int)($_GET['lines'] ?? 360)));
    $query = strtolower(trim((string)($_GET['q'] ?? '')));
    $maxConfidence = max(0, min(1, (float)($_GET['max_conf'] ?? 0.7)));
    $minConfidence = max(0, min($maxConfidence, (float)($_GET['min_conf'] ?? 0.4)));
    $accepted = accepted_lookup();
    $reviewed = reviewed_lookup();
    $log = review_shell('sudo /bin/journalctl -u birdnet_analysis --no-pager -n ' . $lines . ' -o short-iso');
    $rows = [];
    $current = null;
    foreach (explode("\n", $log) as $line) {
        if (preg_match('/Analyzing\s+(\S*StreamData\/([^\/\s]+\.wav))/', $line, $m)) {
            $current = $m[2];
            continue;
        }
        if (!$current) continue;
        if (!preg_match('/\[utils\.analysis\]\[INFO\]\s+([0-9.]+);([0-9.]+)-\((.+),\s*([0-9.]+)\)/', $line, $m)) {
            continue;
        }
        $label = species_common($m[3]);
        $conf = (float)$m[4];
        $hay = strtolower($label['sci'] . ' ' . $label['com']);
        if ($query !== '' && strpos($hay, $query) === false) continue;
        if ($conf < $minConfidence) continue;
        if ($conf > $maxConfidence) continue;
        $key = review_key($current, (float)$m[1], (float)$m[2], $label['sci']);
        $saved = $reviewed[$key] ?? null;
        $rows[] = [
            'id' => sha1($current . '|' . $m[1] . '|' . $m[2] . '|' . $label['sci'] . '|' . $label['com'] . '|' . $conf),
            'file' => $current,
            'audio_url' => '/avian/api/review.php?action=audio&file=' . rawurlencode($current),
            'start_s' => (float)$m[1],
            'end_s' => (float)$m[2],
            'sci' => $label['sci'],
            'com' => $label['com'],
            'confidence' => $conf,
            'accepted' => isset($accepted[$current]),
            'file_exists' => review_audio_file($current) !== null,
            'reviewed_verdict' => $saved['verdict'] ?? '',
            'reviewed_at' => $saved['reviewed_at'] ?? '',
        ];
    }
    usort($rows, function ($a, $b) {
        if ($a['file'] === $b['file']) return $a['start_s'] <=> $b['start_s'];
        return strcmp($b['file'], $a['file']);
    });
    return array_slice($rows, 0, 250);
}

if ($action === 'audio') {
    $path = review_audio_file((string)($_GET['file'] ?? ''));
    if (!$path) {
        http_response_code(404);
        header('Content-Type: text/plain; charset=utf-8');
        echo 'audio chunk not found';
        exit;
    }
    header('Content-Type: audio/wav');
    header('Content-Length: ' . filesize($path));
    header('Accept-Ranges: bytes');
    readfile($path);
    exit;
}

if ($action === 'mark') {
    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') {
        review_json(['error' => 'POST required'], 405);
        exit;
    }
    $body = json_decode((string)file_get_contents('php://input'), true);
    if (!is_array($body)) {
        review_json(['error' => 'invalid json'], 400);
        exit;
    }
    $mark = [
        'reviewed_at' => date('c'),
        'file' => basename((string)($body['file'] ?? '')),
        'start_s' => (float)($body['start_s'] ?? 0),
        'end_s' => (float)($body['end_s'] ?? 0),
        'sci' => (string)($body['sci'] ?? ''),
        'com' => (string)($body['com'] ?? ''),
        'confidence' => (float)($body['confidence'] ?? 0),
        'verdict' => (string)($body['verdict'] ?? ''),
        'note' => substr((string)($body['note'] ?? ''), 0, 240),
    ];
    if (!preg_match('/^(correct|wrong|unsure)$/', $mark['verdict'])) {
        review_json(['error' => 'invalid verdict'], 400);
        exit;
    }
    $mark['clip_path'] = save_review_clip($mark);
    $path = $REVIEW_LOG;
    $ok = @file_put_contents($path, json_encode($mark) . "\n", FILE_APPEND | LOCK_EX);
    if ($ok === false) {
        $path = $REVIEW_FALLBACK_LOG;
        $ok = @file_put_contents($path, json_encode($mark) . "\n", FILE_APPEND | LOCK_EX);
    }
    review_json(['ok' => $ok !== false, 'path' => $path], $ok === false ? 500 : 200);
    exit;
}

if ($action === 'candidates') {
    review_json([
        'candidates' => candidates(),
        'as_of' => date('c'),
    ]);
    exit;
}

review_json(['error' => 'unknown action'], 404);
