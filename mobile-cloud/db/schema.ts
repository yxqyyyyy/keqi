import { integer, sqliteTable, text, uniqueIndex } from "drizzle-orm/sqlite-core";

export const keqiDatasets = sqliteTable("keqi_datasets", {
  id: text("id").primaryKey(),
  ownerId: text("owner_id"),
  sequence: integer("sequence").notNull().default(0),
  tasksJson: text("tasks_json").notNull().default("[]"),
  snapshotHash: text("snapshot_hash").notNull().default(""),
  lastMacSyncAt: text("last_mac_sync_at"),
  platformSyncedAt: text("platform_synced_at"),
});

export const keqiOperations = sqliteTable("keqi_operations", {
  seq: integer("seq").primaryKey({ autoIncrement: true }),
  datasetId: text("dataset_id").notNull(),
  opId: text("op_id").notNull(),
  payloadJson: text("payload_json").notNull(),
  status: text("status").notNull().default("pending"),
  message: text("message"),
  currentJson: text("current_json"),
  createdAt: text("created_at").notNull(),
  resolvedAt: text("resolved_at"),
}, (table) => [uniqueIndex("keqi_operations_dataset_op").on(table.datasetId, table.opId)]);
