<?php

if (!defined('ABSPATH')) {
    exit;
}

function hedgework_wildlife_register_post_type() {
    register_post_type(
        'wildlife_sighting',
        array(
            'labels' => array(
                'name' => 'Wildlife Sightings',
                'singular_name' => 'Wildlife Sighting',
            ),
            'public' => false,
            'show_ui' => true,
            'show_in_rest' => true,
            'supports' => array('title', 'thumbnail', 'custom-fields'),
            'menu_icon' => 'dashicons-camera',
        )
    );
}

function hedgework_wildlife_sighting_meta($post_id) {
    return array(
        'species' => get_post_meta($post_id, 'species', true),
        'display_name' => get_post_meta($post_id, 'display_name', true),
        'confidence' => (float) get_post_meta($post_id, 'confidence', true),
        'camera' => (int) get_post_meta($post_id, 'camera', true),
        'detected_at' => get_post_meta($post_id, 'detected_at', true),
        'bbox' => json_decode((string) get_post_meta($post_id, 'bbox', true), true),
        'image_url' => get_the_post_thumbnail_url($post_id, 'large'),
    );
}
