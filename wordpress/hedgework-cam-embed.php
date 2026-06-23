<?php
/**
 * Plugin Name: Hedgerow Camera Embed
 * Description: Embed a single Pi camera stream with snapshot and fullscreen controls.
 * Version: 1.0.0
 * Author: Hedgerow
 * License: MIT
 *
 * Usage:
 *   [hedgework_cam camera="0" pi_url="https://your-pi.example"]
 *   [hedgework_cam camera="1" pi_url="https://your-pi.example" poll_interval_ms="20000"]
 */

if (!defined('ABSPATH')) {
    exit;
}

define('HEDGEWORK_CAM_EMBED_VERSION', '1.0.0');

function hedgework_cam_embed_register_assets(): void
{
    $base = plugin_dir_url(__FILE__);
    wp_register_style(
        'hedgework-cam-embed',
        $base . 'embed-cam.css',
        [],
        HEDGEWORK_CAM_EMBED_VERSION,
    );
    wp_register_script(
        'hedgework-cam-embed',
        $base . 'embed-cam.js',
        [],
        HEDGEWORK_CAM_EMBED_VERSION,
        true,
    );
}
add_action('wp_enqueue_scripts', 'hedgework_cam_embed_register_assets');

/**
 * @param array<string, string>|string $atts
 */
function hedgework_cam_embed_shortcode($atts): string
{
    $atts = shortcode_atts(
        [
            'camera' => '0',
            'pi_url' => '',
            'poll_interval_ms' => '20000',
        ],
        $atts,
        'hedgework_cam',
    );

    wp_enqueue_style('hedgework-cam-embed');
    wp_enqueue_script('hedgework-cam-embed');

    return sprintf(
        '<div class="hedgework-cam-embed-single" data-pi-url="%s" data-camera="%s" data-poll-interval-ms="%s"></div>',
        esc_attr($atts['pi_url']),
        esc_attr($atts['camera']),
        esc_attr($atts['poll_interval_ms']),
    );
}
add_shortcode('hedgework_cam', 'hedgework_cam_embed_shortcode');
