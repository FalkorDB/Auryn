<?php

namespace App\Models;

enum Status: string
{
    case Active = 'active';
    case Archived = 'archived';

    public function label(): string
    {
        return $this->value;
    }
}
