<?php

namespace App\Models;

trait Timestamps
{
    protected ?string $updatedAt = null;

    public function touch(): void
    {
        $this->updatedAt = 'now';
    }
}
