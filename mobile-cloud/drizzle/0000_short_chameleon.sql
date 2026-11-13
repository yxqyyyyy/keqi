CREATE TABLE `keqi_datasets` (
	`id` text PRIMARY KEY NOT NULL,
	`owner_id` text,
	`sequence` integer DEFAULT 0 NOT NULL,
	`tasks_json` text DEFAULT '[]' NOT NULL,
	`snapshot_hash` text DEFAULT '' NOT NULL,
	`last_mac_sync_at` text,
	`platform_synced_at` text
);
--> statement-breakpoint
CREATE TABLE `keqi_operations` (
	`seq` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`dataset_id` text NOT NULL,
	`op_id` text NOT NULL,
	`payload_json` text NOT NULL,
	`status` text DEFAULT 'pending' NOT NULL,
	`message` text,
	`current_json` text,
	`created_at` text NOT NULL,
	`resolved_at` text
);
--> statement-breakpoint
CREATE UNIQUE INDEX `keqi_operations_dataset_op` ON `keqi_operations` (`dataset_id`,`op_id`);