<?php
/**
 * Plugin Name: Hedgework Wildlife
 * Description: Receives wildlife detections from the PS 20 observation mast and powers the Wildlife Watch public page.
 * Version: 0.1.1
 * Author: Hedgerow
 * License: MIT
 */

if (!defined('ABSPATH')) {
    exit;
}

require_once __DIR__ . '/includes/rest-api.php';
require_once __DIR__ . '/includes/post-type.php';

add_action('init', 'hedgework_wildlife_register_post_type');
add_action('rest_api_init', 'hedgework_wildlife_register_rest_routes');

add_shortcode('hedgework_wildlife_watch', 'hedgework_wildlife_watch_shortcode');

function hedgework_wildlife_enqueue_assets() {
    wp_register_style(
        'hedgework-wildlife-watch',
        plugins_url('assets/wildlife-watch.css', __FILE__),
        array(),
        '0.1.1'
    );
    wp_register_script(
        'hedgework-wildlife-watch',
        plugins_url('assets/wildlife-watch.js', __FILE__),
        array(),
        '0.1.1',
        true
    );
}
add_action('wp_enqueue_scripts', 'hedgework_wildlife_enqueue_assets');

function hedgework_wildlife_watch_shortcode($atts) {
    wp_enqueue_style('hedgework-wildlife-watch');
    wp_enqueue_script('hedgework-wildlife-watch');

    $atts = shortcode_atts(
        array(
            'pi_url' => '',
            'poll_interval_ms' => '20000',
        ),
        $atts,
        'hedgework_wildlife_watch'
    );

    ob_start();
    ?>
    <div
        class="hedgework-wildlife-watch"
        data-pi-url="<?php echo esc_attr($atts['pi_url']); ?>"
        data-poll-interval-ms="<?php echo esc_attr($atts['poll_interval_ms']); ?>"
    >
        <header class="hww-hero">
            <h2>Who visited the hedgerow today?</h2>
            <p class="hww-subtitle">Wildlife spotted by our solar-powered cameras at PS 20.</p>
        </header>
        <section class="hww-counts" aria-label="Today's species counts">
            <div class="hww-counts-grid" id="hww-counts-grid">
                <p class="hww-loading">Loading today's visitors…</p>
            </div>
        </section>
        <section class="hww-gallery" aria-label="Recent wildlife sightings">
            <h3>Recent sightings</h3>
            <div class="hww-gallery-grid" id="hww-gallery-grid">
                <p class="hww-loading">Loading photos…</p>
            </div>
        </section>
    </div>
    <?php
    return ob_get_clean();
}
