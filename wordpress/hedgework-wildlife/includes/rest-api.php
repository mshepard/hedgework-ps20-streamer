<?php

if (!defined('ABSPATH')) {
    exit;
}

function hedgework_wildlife_register_rest_routes() {
    register_rest_route(
        'hedgework/v1',
        '/sighting',
        array(
            'methods' => 'POST',
            'callback' => 'hedgework_wildlife_rest_create_sighting',
            'permission_callback' => 'hedgework_wildlife_rest_can_ingest',
        )
    );

    register_rest_route(
        'hedgework/v1',
        '/stats',
        array(
            'methods' => 'GET',
            'callback' => 'hedgework_wildlife_rest_stats',
            'permission_callback' => '__return_true',
        )
    );

    register_rest_route(
        'hedgework/v1',
        '/recent',
        array(
            'methods' => 'GET',
            'callback' => 'hedgework_wildlife_rest_recent',
            'permission_callback' => '__return_true',
        )
    );
}

function hedgework_wildlife_rest_can_ingest() {
    return current_user_can('upload_files');
}

function hedgework_wildlife_rest_create_sighting(WP_REST_Request $request) {
    $metadata_raw = $request->get_param('metadata');
    if (is_string($metadata_raw)) {
        $metadata = json_decode($metadata_raw, true);
    } else {
        $metadata = $metadata_raw;
    }
    if (!is_array($metadata)) {
        return new WP_Error('invalid_metadata', 'metadata must be JSON', array('status' => 400));
    }

    $files = $request->get_file_params();
    if (empty($files['image'])) {
        return new WP_Error('missing_image', 'image file required', array('status' => 400));
    }

    $species = sanitize_text_field($metadata['species'] ?? 'unknown');
    $display = sanitize_text_field($metadata['display_name'] ?? $species);
    $detected_at = sanitize_text_field($metadata['detected_at'] ?? gmdate('c'));
    $confidence = isset($metadata['confidence']) ? (float) $metadata['confidence'] : 0.0;
    $camera = isset($metadata['camera']) ? (int) $metadata['camera'] : 0;
    $bbox = isset($metadata['bbox']) ? wp_json_encode($metadata['bbox']) : '';

    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';
    require_once ABSPATH . 'wp-admin/includes/image.php';

    $attachment_id = media_handle_sideload($files['image'], 0, $display);
    if (is_wp_error($attachment_id)) {
        return $attachment_id;
    }

    $post_id = wp_insert_post(
        array(
            'post_type' => 'wildlife_sighting',
            'post_status' => 'publish',
            'post_title' => $display . ' — ' . $detected_at,
            'meta_input' => array(
                'species' => $species,
                'display_name' => $display,
                'confidence' => $confidence,
                'camera' => $camera,
                'detected_at' => $detected_at,
                'bbox' => $bbox,
            ),
        )
    );

    if (is_wp_error($post_id)) {
        return $post_id;
    }

    set_post_thumbnail($post_id, $attachment_id);

    return rest_ensure_response(
        array(
            'id' => $post_id,
            'media_id' => $attachment_id,
        )
    );
}

function hedgework_wildlife_period_start($period) {
    $tz = wp_timezone();
    $now = new DateTimeImmutable('now', $tz);
    if ($period === 'week') {
        return $now->modify('-7 days')->format('Y-m-d\TH:i:s');
    }
    return $now->format('Y-m-d') . 'T00:00:00';
}

function hedgework_wildlife_rest_stats(WP_REST_Request $request) {
    $period = sanitize_text_field($request->get_param('period') ?: 'today');
    $since = hedgework_wildlife_period_start($period);

    $query = new WP_Query(
        array(
            'post_type' => 'wildlife_sighting',
            'posts_per_page' => -1,
            'fields' => 'ids',
            'meta_query' => array(
                array(
                    'key' => 'detected_at',
                    'value' => $since,
                    'compare' => '>=',
                    'type' => 'CHAR',
                ),
            ),
        )
    );

    $counts = array();
    foreach ($query->posts as $post_id) {
        $species = get_post_meta($post_id, 'species', true);
        $display = get_post_meta($post_id, 'display_name', true);
        $key = $species ?: 'unknown';
        if (!isset($counts[$key])) {
            $counts[$key] = array(
                'species' => $key,
                'display_name' => $display ?: $key,
                'count' => 0,
            );
        }
        $counts[$key]['count'] += 1;
    }

    usort(
        $counts,
        function ($a, $b) {
            return $b['count'] <=> $a['count'];
        }
    );

    return rest_ensure_response(array('counts' => array_values($counts)));
}

function hedgework_wildlife_rest_recent(WP_REST_Request $request) {
    $limit = max(1, min(50, (int) $request->get_param('limit') ?: 12));

    $query = new WP_Query(
        array(
            'post_type' => 'wildlife_sighting',
            'posts_per_page' => $limit,
            'orderby' => 'meta_value',
            'meta_key' => 'detected_at',
            'order' => 'DESC',
        )
    );

    $items = array();
    foreach ($query->posts as $post) {
        $items[] = hedgework_wildlife_sighting_meta($post->ID);
    }

    return rest_ensure_response(array('sightings' => $items));
}
