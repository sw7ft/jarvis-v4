<?php
/**
 * Generic form handler for website agents.
 * Handles contact form, newsletter signup, chat widget, and survey.
 * Posts to Rocket.Chat via Incoming Webhook and logs all submissions.
 *
 * Form types: contact | newsletter | chat (floating popup: email + message) | takeaction | survey
 *
 * TEMPLATE: website-agent — customize title strings and log paths as needed.
 */

header('Content-Type: application/json');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(204);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['ok' => false, 'error' => 'Method not allowed']);
    exit;
}

// Load webhook URL from config (same directory)
$configFile = __DIR__ . '/contact-webhook-config.php';
if (!file_exists($configFile)) {
    error_log('form-submit: Missing contact-webhook-config.php');
    http_response_code(500);
    echo json_encode(['ok' => false, 'error' => 'Form is not configured. Please try again later.']);
    exit;
}

$config = include $configFile;
$webhookUrl = $config['webhook_url'] ?? '';

if (empty($webhookUrl)) {
    error_log('form-submit: webhook_url not set in config');
    http_response_code(500);
    echo json_encode(['ok' => false, 'error' => 'Form is not configured. Please try again later.']);
    exit;
}

// Log file: ~/logs/form-submissions.log (one level above web root = user home)
$logDir = dirname(__DIR__, 1) . '/logs';
$logFile = $logDir . '/form-submissions.log';

// Accept JSON or form-urlencoded
$input = [];
$contentType = $_SERVER['HTTP_CONTENT_TYPE'] ?? '';
if (strpos($contentType, 'application/json') !== false) {
    $raw = file_get_contents('php://input');
    $input = json_decode($raw, true) ?: [];
} else {
    $input = $_POST;
}

$firstName = trim($input['firstName'] ?? $input['first_name'] ?? '');
$lastName  = trim($input['lastName'] ?? $input['last_name'] ?? '');
$email     = trim($input['email'] ?? '');
$phone     = trim($input['phone'] ?? '');
$community = trim($input['community'] ?? '');
$subject   = trim($input['subject'] ?? '');
$message   = trim($input['message'] ?? '');

// ── Spam prevention (PHP-only, no external services) ──
$honeypot = trim($input['website'] ?? $input['url'] ?? $input['company_name'] ?? '');
if (!empty($honeypot)) {
    http_response_code(200);
    echo json_encode(['ok' => true]);
    exit;
}

$formLoadTime = (float)($input['formLoadTime'] ?? 0);
if ($formLoadTime > 0 && (microtime(true) - $formLoadTime) < 3) {
    http_response_code(429);
    echo json_encode(['ok' => false, 'error' => 'Please wait a moment before submitting.']);
    exit;
}

$clientIp = $_SERVER['HTTP_X_FORWARDED_FOR'] ?? $_SERVER['HTTP_X_REAL_IP'] ?? $_SERVER['REMOTE_ADDR'] ?? '';
$clientIp = trim(explode(',', $clientIp)[0]);
if (!empty($clientIp)) {
    $rateFile = sys_get_temp_dir() . '/formrate_' . md5($clientIp . __DIR__);
    $now = time();
    $window = 3600;
    $maxPerHour = 5;
    $submissions = [];
    if (file_exists($rateFile)) {
        $raw = @file_get_contents($rateFile);
        $submissions = $raw ? array_filter(array_map('intval', explode("\n", trim($raw)))) : [];
    }
    $submissions = array_filter($submissions, fn($t) => $t > $now - $window);
    if (count($submissions) >= $maxPerHour) {
        http_response_code(429);
        echo json_encode(['ok' => false, 'error' => 'Too many submissions. Please try again later.']);
        exit;
    }
    $submissions[] = $now;
    @file_put_contents($rateFile, implode("\n", $submissions) . "\n", LOCK_EX);
}

if (!empty($message)) {
    $urlCount = preg_match_all('#https?://\S+#', $message, $m) ? count($m[0]) : 0;
    if ($urlCount > 2) {
        http_response_code(400);
        echo json_encode(['ok' => false, 'error' => 'Please limit links in your message.']);
        exit;
    }
    if (strlen($message) > 2000) {
        http_response_code(400);
        echo json_encode(['ok' => false, 'error' => 'Message is too long.']);
        exit;
    }
}

// Detect form type
$formType = trim($input['formType'] ?? '');
$isContact = ($formType === 'contact') || (!empty($message) && $formType !== 'takeaction' && $formType !== 'survey');
$isChat = ($formType === 'chat');
$isTakeAction = ($formType === 'takeaction');
$isSurvey = ($formType === 'survey');

if ($isChat) {
    $isContact = false;
    $isTakeAction = false;
    $isSurvey = false;
    if (empty($email) || empty($message)) {
        http_response_code(400);
        echo json_encode(['ok' => false, 'error' => 'Please enter your email and message.']);
        exit;
    }
} elseif ($formType === 'contact' || (!empty($message) && !$isTakeAction && !$isSurvey)) {
    $isContact = true;
    $isTakeAction = false;
    $isSurvey = false;
    if (empty($firstName) || empty($lastName) || empty($email)) {
        http_response_code(400);
        echo json_encode(['ok' => false, 'error' => 'Please fill in all required fields.']);
        exit;
    }
} elseif ($isTakeAction) {
    $isContact = false;
    $isSurvey = false;
    if (empty($firstName) || empty($email)) {
        http_response_code(400);
        echo json_encode(['ok' => false, 'error' => 'Please fill in first name and email.']);
        exit;
    }
} elseif ($isSurvey) {
    $isContact = false;
    $isTakeAction = false;
    if (empty($email)) {
        http_response_code(400);
        echo json_encode(['ok' => false, 'error' => 'Please enter your email address.']);
        exit;
    }
} else {
    $isContact = false;
    $isTakeAction = false;
    if (empty($email)) {
        http_response_code(400);
        echo json_encode(['ok' => false, 'error' => 'Please enter your email address.']);
        exit;
    }
}

if (!empty($email) && !filter_var($email, FILTER_VALIDATE_EMAIL)) {
    http_response_code(400);
    echo json_encode(['ok' => false, 'error' => 'Please enter a valid email address.']);
    exit;
}

$interests = '';
if ($isTakeAction && isset($input['interests'])) {
    $interests = trim(is_array($input['interests']) ? implode(', ', $input['interests']) : (string)$input['interests']);
}

$issues = '';
$topPriority = trim($input['topPriority'] ?? '');
$rightTrack = trim($input['rightTrack'] ?? '');
$comments = trim($input['comments'] ?? '');
if ($isSurvey && isset($input['issues'])) {
    $issues = trim(is_array($input['issues']) ? implode(', ', $input['issues']) : (string)$input['issues']);
}

// Derive site label from HTTP_HOST or referer
$siteLabel = $_SERVER['HTTP_HOST'] ?? parse_url($_SERVER['HTTP_REFERER'] ?? '', PHP_URL_HOST) ?? 'website';

// Build Rocket.Chat message
if ($isChat) {
    $lines = [
        "**New message from chat widget**",
        "",
        "**Email:** {$email}",
        "",
        "**Message:**",
        $message,
        "",
        "**Source:** " . ($_SERVER['HTTP_REFERER'] ?? 'unknown'),
    ];
    $title = $siteLabel . ' Chat Widget';
} elseif ($isTakeAction) {
    $name = trim(($firstName . ' ' . $lastName));
    $lines = ["**New Take Action / Get Involved signup**", ""];
    if (!empty($name)) $lines[] = "**Name:** {$name}";
    $lines[] = "**Email:** {$email}";
    if (!empty($community)) $lines[] = "**Community:** {$community}";
    if (!empty($interests)) $lines[] = "**Interests:** {$interests}";
    $lines[] = "**Source:** " . ($_SERVER['HTTP_REFERER'] ?? 'unknown');
    $title = $siteLabel . ' Take Action';
} elseif ($isSurvey) {
    $name = trim(($firstName . ' ' . $lastName));
    $lines = ["**New survey submission**", ""];
    if (!empty($name)) $lines[] = "**Name:** {$name}";
    $lines[] = "**Email:** {$email}";
    if (!empty($community)) $lines[] = "**Community:** {$community}";
    if (!empty($rightTrack)) $lines[] = "**Right track / Wrong track:** {$rightTrack}";
    if (!empty($issues)) $lines[] = "**Issues that matter:** {$issues}";
    if (!empty($topPriority)) $lines[] = "**Top priority:** {$topPriority}";
    if (!empty($comments)) {
        $lines[] = "";
        $lines[] = "**Comments:**";
        $lines[] = $comments;
    }
    $lines[] = "";
    $lines[] = "**Source:** " . ($_SERVER['HTTP_REFERER'] ?? 'unknown');
    $title = $siteLabel . ' Survey';
} elseif ($isContact) {
    $lines = [
        "**New contact form submission**",
        "",
        "**Name:** {$firstName} {$lastName}",
        "**Email:** {$email}",
    ];
    if (!empty($phone)) $lines[] = "**Phone:** {$phone}";
    if (!empty($community)) $lines[] = "**Community:** {$community}";
    $lines[] = "**Subject:** {$subject}";
    $lines[] = "";
    $lines[] = "**Message:**";
    $lines[] = $message;
    $title = $siteLabel . ' Contact Form';
} else {
    $name = trim(($firstName . ' ' . $lastName));
    $lines = ["**New newsletter signup**", ""];
    if (!empty($name)) $lines[] = "**Name:** {$name}";
    $lines[] = "**Email:** {$email}";
    if (!empty($community)) $lines[] = "**Community:** {$community}";
    $lines[] = "**Source:** " . ($_SERVER['HTTP_REFERER'] ?? 'unknown');
    $title = $siteLabel . ' Newsletter Signup';
}
$text = implode("\n", $lines);

$payload = [
    'text' => $text,
    'attachments' => [
        ['color' => '#1e3a5f', 'title' => $title],
    ],
];

// Log to file
$logType = $isChat ? 'chat' : ($isSurvey ? 'survey' : ($isTakeAction ? 'takeaction' : ($isContact ? 'contact' : 'newsletter')));
$logPayload = [
    'email' => $email,
    'firstName' => $firstName,
    'lastName' => $lastName,
    'phone' => $phone ?: null,
    'community' => $community ?: null,
    'subject' => $subject ?: null,
    'message' => $isContact ? $message : null,
];
if ($isSurvey) {
    $logPayload['rightTrack'] = $rightTrack ?: null;
    $logPayload['issues'] = $issues ?: null;
    $logPayload['topPriority'] = $topPriority ?: null;
    $logPayload['comments'] = $comments ?: null;
}
$logEntry = date('Y-m-d H:i:s') . "\t" . $logType . "\t" . json_encode($logPayload) . "\n";
if (!is_dir($logDir)) {
    @mkdir($logDir, 0755, true);
}
@file_put_contents($logFile, $logEntry, FILE_APPEND | LOCK_EX);

// Newsletter signups: append to email.json (for mailing list)
if ($logType === 'newsletter') {
    $emailJsonPath = __DIR__ . '/email.json';
    $entry = [
        'email' => $email,
        'firstName' => $firstName ?: null,
        'lastName' => $lastName ?: null,
        'community' => $community ?: null,
        'source' => $_SERVER['HTTP_REFERER'] ?? null,
        'timestamp' => date('c'),
    ];
    $list = [];
    if (file_exists($emailJsonPath)) {
        $raw = @file_get_contents($emailJsonPath);
        if ($raw) {
            $decoded = json_decode($raw, true);
            if (is_array($decoded)) $list = $decoded;
        }
    }
    $list[] = $entry;
    @file_put_contents($emailJsonPath, json_encode($list, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), LOCK_EX);
}

// Send to Rocket.Chat (using file_get_contents - curl may not be available)
$body = json_encode($payload);
$opts = [
    'http' => [
        'method' => 'POST',
        'header' => "Content-Type: application/json\r\nContent-Length: " . strlen($body) . "\r\nUser-Agent: WebsiteAgent-ContactForm/1.0\r\n",
        'content' => $body,
        'timeout' => 15,
        'ignore_errors' => true,
    ],
    'ssl' => [
        'verify_peer' => true,
        'verify_peer_name' => true,
    ],
];
$context = stream_context_create($opts);
$response = @file_get_contents($webhookUrl, false, $context);

$httpCode = 0;
if (isset($http_response_header) && !empty($http_response_header)) {
    preg_match('/HTTP\/\d\.\d\s+(\d+)/', $http_response_header[0], $m);
    $httpCode = (int)($m[1] ?? 0);
}

if ($httpCode >= 200 && $httpCode < 300) {
    echo json_encode(['ok' => true]);
    exit;
}

http_response_code(500);
echo json_encode(['ok' => false, 'error' => 'Could not send your message. Please try again later.']);
