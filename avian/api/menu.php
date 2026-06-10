<?php
// AvianVisitors - drawer menu items.
//
// Returns the list of links shown in the side drawer when a user clicks
// the menu button. The live JS expects {items: [{label, href, native}]}.
//
// Default LAN deploy with blank CADDY_PWD: returns items immediately.
// If /etc/birdnet/birdnet.conf has CADDY_PWD set, validate the frontend's
// Basic auth header here so the drawer can unlock without extra Caddy rules.

declare(strict_types=1);

session_name('avian_session');
session_set_cookie_params([
    'lifetime' => 60 * 60 * 24 * 30,
    'path' => '/',
    'httponly' => true,
    'samesite' => 'Lax',
]);
session_start();

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

function av_config_value(string $name): string {
    $path = '/etc/birdnet/birdnet.conf';
    if (!is_readable($path)) return '';
    $raw = (string)file_get_contents($path);
    $pattern = '/^' . preg_quote($name, '/') . '=(.*)$/m';
    if (!preg_match($pattern, $raw, $m)) return '';
    $value = trim($m[1]);
    if (
        strlen($value) >= 2 &&
        (($value[0] === '"' && substr($value, -1) === '"') ||
         ($value[0] === "'" && substr($value, -1) === "'"))
    ) {
        $value = substr($value, 1, -1);
    }
    return $value;
}

function av_config_password(): string {
    return av_config_value('CADDY_PWD');
}

function av_is_developer_client(): bool {
    $allowed = av_config_value('AVIAN_DEV_IPS');
    if ($allowed === '') return false;
    $client = (string)($_SERVER['REMOTE_ADDR'] ?? '');
    if ($client === '') return false;
    $ips = preg_split('/[\s,]+/', $allowed, -1, PREG_SPLIT_NO_EMPTY);
    return in_array($client, $ips, true);
}

function av_basic_credentials(): array {
    if (!empty($_SERVER['PHP_AUTH_USER']) || !empty($_SERVER['PHP_AUTH_PW'])) {
        return [(string)($_SERVER['PHP_AUTH_USER'] ?? ''), (string)($_SERVER['PHP_AUTH_PW'] ?? '')];
    }
    $auth = (string)($_SERVER['HTTP_AUTHORIZATION'] ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '');
    if (!preg_match('/^Basic\s+(.+)$/i', $auth, $m)) return ['', ''];
    $decoded = base64_decode($m[1], true);
    if ($decoded === false || strpos($decoded, ':') === false) return ['', ''];
    return explode(':', $decoded, 2);
}

function av_unauthorized(): void {
    http_response_code(401);
    echo json_encode(['error' => 'unauthorized']);
    exit;
}

$expectedPassword = av_config_password();
if ($expectedPassword !== '' && !av_is_developer_client()) {
    $passwordHash = hash('sha256', $expectedPassword);
    $sessionOk = (($_SESSION['av_password_hash'] ?? '') === $passwordHash);
    if (!$sessionOk) {
        [$user, $password] = av_basic_credentials();
        if ($user !== 'birdnet' || !hash_equals($expectedPassword, $password)) {
            av_unauthorized();
        }
        $_SESSION['av_password_hash'] = $passwordHash;
    }
}

// All four items are in-app overlays. `native: true` tells the FE to
// route via `#admin=<section>` rather than opening a new window. We
// deliberately don't link out to BirdNET-Pi's stock pages - those stay
// reachable at /index.php, and the github link lives in the drawer
// footer next to "built by teddy".
echo json_encode([
    'items' => [
        ['label' => 'settings', 'href' => '/#admin=settings', 'native' => true],
        ['label' => 'system',   'href' => '/#admin=system',   'native' => true],
        ['label' => 'review',   'href' => '/#admin=review',   'native' => true],
        ['label' => 'logs',     'href' => '/#admin=logs',     'native' => true],
        ['label' => 'tools',    'href' => '/#admin=tools',    'native' => true],
    ],
]);
